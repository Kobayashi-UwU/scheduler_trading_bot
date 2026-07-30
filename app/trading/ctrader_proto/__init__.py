# Generated protobuf message classes for the cTrader Open API. Vendored (not pip
# installed) because the official `ctrader-open-api` package hard-pins Twisted and
# an old protobuf (3.20.1, no Python 3.12 wheel) we don't want or need — we only
# use the raw *_pb2.py message classes with our own synchronous socket client
# (see app/trading/ctrader_client.py).
#
# Regenerate after a schema change:
#   pip install grpcio-tools==1.83.0
#   curl -sO https://raw.githubusercontent.com/spotware/openapi-proto-messages/<commit>/OpenApiCommonMessages.proto
#   curl -sO https://raw.githubusercontent.com/spotware/openapi-proto-messages/<commit>/OpenApiCommonModelMessages.proto
#   curl -sO https://raw.githubusercontent.com/spotware/openapi-proto-messages/<commit>/OpenApiMessages.proto
#   curl -sO https://raw.githubusercontent.com/spotware/openapi-proto-messages/<commit>/OpenApiModelMessages.proto
#   python -m grpc_tools.protoc -I . --python_out=app/trading/ctrader_proto \
#       OpenApiCommonMessages.proto OpenApiCommonModelMessages.proto \
#       OpenApiMessages.proto OpenApiModelMessages.proto
#
# Source: https://github.com/spotware/openapi-proto-messages @ 3fd8bddfbe0cfc2ecfda079623dc4e498af11e66
# Generated with grpcio-tools==1.83.0 (protobuf==7.35.1 runtime).
