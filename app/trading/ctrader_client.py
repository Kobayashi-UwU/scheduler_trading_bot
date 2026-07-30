"""Minimal synchronous client for the cTrader Open API (Spotware), used to mirror
real-tier sim positions onto live cTrader/Axiory demo accounts.

Deliberately NOT the official `ctrader-open-api` pip package: that package hard-pins
Twisted plus protobuf==3.20.1 (no Python 3.12 wheel), which is exactly the kind of
heavyweight dependency this replaces the old MT5/Wine-container executor to avoid.
Instead this talks raw protobuf over a TLS socket using the vendored message classes
in `app/trading/ctrader_proto/` (see that package's docstring for how to regenerate).

Protocol basics (verified against the official .proto sources and the reference
client's transport, not just prose docs):
  - TLS TCP to demo.ctraderapi.com:5035 or live.ctraderapi.com:5035, port 5035 is
    fixed for the Protobuf variant.
  - Each frame is a 4-byte big-endian length prefix followed by a serialized
    ProtoMessage envelope (payloadType, payload bytes, clientMsgId).
  - Sequence: connect -> ProtoOAApplicationAuthReq (once) -> ProtoOAAccountAuthReq
    per trading account -> trading/data requests. One connection can authorize
    many accounts of the same demo/live environment.
  - The server disconnects after >30s with no messages; ProtoHeartbeatEvent keeps
    it alive. We open a fresh connection per reconciliation pass (a few seconds of
    work for 1-2 accounts), so no heartbeat thread is needed — `_recv` just
    auto-replies if the server happens to send one while we're waiting.
  - MARKET orders do NOT accept stopLoss/takeProfit in ProtoOANewOrderReq itself
    (the .proto comments explicitly say "Not supported for MARKET orders") — SL/TP
    must be attached after the fill via a separate ProtoOAAmendPositionSLTPReq,
    using the real fill price from the execution event's position.price. There is
    briefly no protective stop between fill and amend; unavoidable given the
    protocol and acceptable for this bot's size/purpose.
  - Order/close confirmation is asynchronous via ProtoOAExecutionEvent, not a
    dedicated Res message — we read frames in a bounded loop until we see one (or
    an error), which is safe because this is a fresh connection used for nothing
    else at the time.
"""

import logging
import socket
import ssl
import struct
import time
from dataclasses import dataclass

from app.trading.ctrader_proto import (
    OpenApiCommonMessages_pb2 as common,
    OpenApiCommonModelMessages_pb2 as common_model,
    OpenApiMessages_pb2 as oa,
    OpenApiModelMessages_pb2 as oa_model,
)

log = logging.getLogger("ctrader_client")

PT = oa_model.ProtoOAPayloadType
CPT = common_model.ProtoPayloadType

_HEARTBEAT_TYPE = CPT.Value("HEARTBEAT_EVENT")
_ERROR_TYPE = CPT.Value("ERROR_RES")
_OA_ERROR_TYPE = PT.Value("PROTO_OA_ERROR_RES")
_EXECUTION_EVENT_TYPE = PT.Value("PROTO_OA_EXECUTION_EVENT")

_BUY = oa_model.ProtoOATradeSide.Value("BUY")
_SELL = oa_model.ProtoOATradeSide.Value("SELL")
_MARKET = oa_model.ProtoOAOrderType.Value("MARKET")
_POSITION_OPEN = oa_model.ProtoOAPositionStatus.Value("POSITION_STATUS_OPEN")
_ORDER_FILLED = oa_model.ProtoOAExecutionType.Value("ORDER_FILLED")
_ORDER_REJECTED = oa_model.ProtoOAExecutionType.Value("ORDER_REJECTED")
_ORDER_CANCEL_REJECTED = oa_model.ProtoOAExecutionType.Value("ORDER_CANCEL_REJECTED")


class CTraderError(Exception):
    pass


@dataclass
class SymbolMeta:
    symbol_id: int
    digits: int
    min_volume: int  # all volume fields are in "cents of a unit": units * 100
    max_volume: int
    step_volume: int
    lot_size: int  # units (e.g. troy oz) per 1.00 lot, in the same cents scale


@dataclass
class BrokerPosition:
    position_id: int
    symbol_id: int
    direction: str  # "LONG" / "SHORT"
    volume: int  # raw cents-of-unit, convert via SymbolMeta.lot_size
    label: str


@dataclass
class ExecResult:
    ok: bool
    position_id: int | None = None
    fill_price: float | None = None
    error: str = ""


@dataclass
class TraderAccount:
    ctid_trader_account_id: int
    trader_login: int
    is_live: bool


class CTraderClient:
    def __init__(self, host: str, port: int, client_id: str, client_secret: str, timeout: float = 10.0):
        self._host = host
        self._port = port
        self._client_id = client_id
        self._client_secret = client_secret
        self._timeout = timeout
        self._sock: ssl.SSLSocket | None = None
        self._msg_counter = 0

    # --- connection lifecycle -------------------------------------------------

    def connect(self) -> bool:
        try:
            raw = socket.create_connection((self._host, self._port), timeout=self._timeout)
            ctx = ssl.create_default_context()
            self._sock = ctx.wrap_socket(raw, server_hostname=self._host)
            self._sock.settimeout(self._timeout)
        except OSError as exc:
            log.error("cannot connect to %s:%s: %s", self._host, self._port, exc)
            self._sock = None
            return False

        try:
            req = oa.ProtoOAApplicationAuthReq(clientId=self._client_id, clientSecret=self._client_secret)
            self._call(req, PT.Value("PROTO_OA_APPLICATION_AUTH_REQ"),
                       PT.Value("PROTO_OA_APPLICATION_AUTH_RES"), oa.ProtoOAApplicationAuthRes)
            return True
        except CTraderError as exc:
            log.error("application auth failed: %s", exc)
            self.close()
            return False

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def authorize_account(self, ctid: int, access_token: str) -> bool:
        req = oa.ProtoOAAccountAuthReq(ctidTraderAccountId=ctid, accessToken=access_token)
        try:
            self._call(req, PT.Value("PROTO_OA_ACCOUNT_AUTH_REQ"),
                       PT.Value("PROTO_OA_ACCOUNT_AUTH_RES"), oa.ProtoOAAccountAuthRes)
            return True
        except CTraderError as exc:
            log.error("account auth failed for ctid %s: %s", ctid, exc)
            return False

    # --- account discovery (used by scripts/ctrader_oauth_setup.py) ----------

    def get_accounts_by_token(self, access_token: str) -> list[TraderAccount]:
        req = oa.ProtoOAGetAccountListByAccessTokenReq(accessToken=access_token)
        res = self._call(req, PT.Value("PROTO_OA_GET_ACCOUNTS_BY_ACCESS_TOKEN_REQ"),
                          PT.Value("PROTO_OA_GET_ACCOUNTS_BY_ACCESS_TOKEN_RES"),
                          oa.ProtoOAGetAccountListByAccessTokenRes)
        return [
            TraderAccount(
                ctid_trader_account_id=a.ctidTraderAccountId,
                trader_login=a.traderLogin,
                is_live=a.isLive,
            )
            for a in res.ctidTraderAccount
        ]

    # --- symbols ---------------------------------------------------------------

    def get_symbol(self, ctid: int, name: str) -> SymbolMeta | None:
        list_req = oa.ProtoOASymbolsListReq(ctidTraderAccountId=ctid)
        list_res = self._call(list_req, PT.Value("PROTO_OA_SYMBOLS_LIST_REQ"),
                               PT.Value("PROTO_OA_SYMBOLS_LIST_RES"), oa.ProtoOASymbolsListRes)
        symbol_id = next((s.symbolId for s in list_res.symbol if s.symbolName == name), None)
        if symbol_id is None:
            return None

        id_req = oa.ProtoOASymbolByIdReq(ctidTraderAccountId=ctid, symbolId=[symbol_id])
        id_res = self._call(id_req, PT.Value("PROTO_OA_SYMBOL_BY_ID_REQ"),
                             PT.Value("PROTO_OA_SYMBOL_BY_ID_RES"), oa.ProtoOASymbolByIdRes)
        if not id_res.symbol:
            return None
        sym = id_res.symbol[0]
        return SymbolMeta(
            symbol_id=sym.symbolId,
            digits=sym.digits,
            min_volume=sym.minVolume,
            max_volume=sym.maxVolume,
            step_volume=sym.stepVolume,
            lot_size=sym.lotSize,
        )

    # --- positions ---------------------------------------------------------------

    def positions(self, ctid: int) -> list[BrokerPosition]:
        req = oa.ProtoOAReconcileReq(ctidTraderAccountId=ctid)
        res = self._call(req, PT.Value("PROTO_OA_RECONCILE_REQ"),
                          PT.Value("PROTO_OA_RECONCILE_RES"), oa.ProtoOAReconcileRes)
        out = []
        for p in res.position:
            if p.positionStatus != _POSITION_OPEN:
                continue
            td = p.tradeData
            out.append(BrokerPosition(
                position_id=p.positionId,
                symbol_id=td.symbolId,
                direction="LONG" if td.tradeSide == _BUY else "SHORT",
                volume=td.volume,
                label=td.label or "",
            ))
        return out

    # --- trading ---------------------------------------------------------------

    def new_market_order(self, ctid: int, symbol_id: int, direction: str, volume: int, label: str) -> ExecResult:
        """Places a plain market order (no SL/TP — unsupported on MARKET orders by
        the API itself). Caller must follow up with amend_sl_tp once filled."""
        req = oa.ProtoOANewOrderReq(
            ctidTraderAccountId=ctid,
            symbolId=symbol_id,
            orderType=_MARKET,
            tradeSide=_BUY if direction == "LONG" else _SELL,
            volume=volume,
            label=label[:100],
            comment=label[:512],
        )
        self._send(req, PT.Value("PROTO_OA_NEW_ORDER_REQ"))
        return self._wait_fill(ctid)

    def close_position(self, ctid: int, position_id: int, volume: int) -> ExecResult:
        req = oa.ProtoOAClosePositionReq(ctidTraderAccountId=ctid, positionId=position_id, volume=volume)
        self._send(req, PT.Value("PROTO_OA_CLOSE_POSITION_REQ"))
        return self._wait_fill(ctid)

    def amend_sl_tp(self, ctid: int, position_id: int, stop_loss: float, take_profit: float) -> None:
        """Best-effort: the API doesn't clearly document a distinct execution-type
        for this request, so we drain briefly and log whatever comes back rather
        than treat an ambiguous response as a hard failure — the position is
        already open regardless of whether this attaches cleanly."""
        req = oa.ProtoOAAmendPositionSLTPReq(
            ctidTraderAccountId=ctid, positionId=position_id,
            stopLoss=stop_loss, takeProfit=take_profit,
        )
        self._send(req, PT.Value("PROTO_OA_AMEND_POSITION_SLTP_REQ"))
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            try:
                payload_type, payload = self._recv()
            except CTraderError:
                return
            if payload_type in (_ERROR_TYPE, _OA_ERROR_TYPE):
                log.warning("amend SL/TP for position %s: %s", position_id, self._error_text(payload_type, payload))
                return
            if payload_type == _EXECUTION_EVENT_TYPE:
                return  # something came back referencing our request; good enough

    def _wait_fill(self, ctid: int) -> ExecResult:
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            try:
                payload_type, payload = self._recv()
            except CTraderError as exc:
                return ExecResult(ok=False, error=str(exc))
            if payload_type in (_ERROR_TYPE, _OA_ERROR_TYPE):
                return ExecResult(ok=False, error=self._error_text(payload_type, payload))
            if payload_type != _EXECUTION_EVENT_TYPE:
                continue
            ev = oa.ProtoOAExecutionEvent()
            ev.ParseFromString(payload)
            if ev.ctidTraderAccountId != ctid:
                continue
            if ev.executionType in (_ORDER_REJECTED, _ORDER_CANCEL_REJECTED):
                return ExecResult(ok=False, error=ev.errorCode or "order rejected")
            if ev.executionType == _ORDER_FILLED:
                pos = ev.position if ev.HasField("position") else None
                return ExecResult(
                    ok=True,
                    position_id=pos.positionId if pos is not None else None,
                    fill_price=pos.price if pos is not None and pos.HasField("price") else None,
                )
            # ORDER_ACCEPTED or anything else: intermediate, keep waiting
        return ExecResult(ok=False, error="timeout waiting for execution event")

    # --- wire framing ---------------------------------------------------------------

    def _next_msg_id(self) -> str:
        self._msg_counter += 1
        return str(self._msg_counter)

    def _send(self, message, payload_type: int) -> None:
        if self._sock is None:
            raise CTraderError("not connected")
        envelope = common.ProtoMessage(
            payloadType=payload_type,
            payload=message.SerializeToString(),
            clientMsgId=self._next_msg_id(),
        )
        data = envelope.SerializeToString()
        try:
            self._sock.sendall(struct.pack(">I", len(data)) + data)
        except OSError as exc:
            raise CTraderError(f"send failed: {exc}") from exc

    def _recv_exact(self, n: int) -> bytes:
        chunks = []
        got = 0
        while got < n:
            try:
                chunk = self._sock.recv(n - got)
            except OSError as exc:
                raise CTraderError(f"recv failed: {exc}") from exc
            if not chunk:
                raise CTraderError("connection closed by server")
            chunks.append(chunk)
            got += len(chunk)
        return b"".join(chunks)

    def _recv(self) -> tuple[int, bytes]:
        """Read one frame; transparently answer heartbeats and keep waiting."""
        header = self._recv_exact(4)
        (length,) = struct.unpack(">I", header)
        raw = self._recv_exact(length)
        envelope = common.ProtoMessage()
        envelope.ParseFromString(raw)
        if envelope.payloadType == _HEARTBEAT_TYPE:
            self._send(common.ProtoHeartbeatEvent(), _HEARTBEAT_TYPE)
            return self._recv()
        return envelope.payloadType, envelope.payload

    def _call(self, message, req_type: int, res_type: int, res_cls):
        self._send(message, req_type)
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            payload_type, payload = self._recv()
            if payload_type == res_type:
                out = res_cls()
                out.ParseFromString(payload)
                return out
            if payload_type in (_ERROR_TYPE, _OA_ERROR_TYPE):
                raise CTraderError(self._error_text(payload_type, payload))
            # some other push event arrived first on this fresh connection — ignore, keep waiting
        raise CTraderError(f"timeout waiting for response type {res_type}")

    @staticmethod
    def _error_text(payload_type: int, payload: bytes) -> str:
        if payload_type == _OA_ERROR_TYPE:
            err = oa.ProtoOAErrorRes()
            err.ParseFromString(payload)
            return f"{err.errorCode}: {err.description}"
        err = common.ProtoErrorRes()
        err.ParseFromString(payload)
        return f"{err.errorCode}: {err.description}"


def host_for(environment: str) -> str:
    return "live.ctraderapi.com" if environment == "live" else "demo.ctraderapi.com"


CTRADER_PORT = 5035


def to_volume(lots: float, symbol: SymbolMeta) -> int:
    """lots -> raw protocol volume (cents-of-unit), derived from the broker's own
    lotSize rather than assuming any fixed contract size."""
    return round(lots * symbol.lot_size)


def to_lots(volume: int, symbol: SymbolMeta) -> float:
    if symbol.lot_size <= 0:
        return 0.0
    return volume / symbol.lot_size
