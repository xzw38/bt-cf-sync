FROM alpine:3.20

RUN apk add --no-cache bash ca-certificates coreutils curl grep jq sed

COPY sync.sh /usr/local/bin/bt-cf-sync
RUN chmod +x /usr/local/bin/bt-cf-sync

VOLUME ["/data"]

CMD ["/usr/local/bin/bt-cf-sync"]
