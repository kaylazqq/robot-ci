FROM golang:1.26.5-alpine AS build

WORKDIR /src
COPY go.mod ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/echo-router ./cmd/router/

FROM alpine:3.22
RUN addgroup -S echo && adduser -S -G echo echo \
    && apk add --no-cache ca-certificates
COPY --from=build /out/echo-router /usr/local/bin/echo-router
USER echo
EXPOSE 31000
ENTRYPOINT ["/usr/local/bin/echo-router"]
