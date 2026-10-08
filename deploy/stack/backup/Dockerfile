FROM alpine:3.20
RUN apk add --no-cache postgresql16-client age rclone tzdata bash coreutils
COPY backup.sh /usr/local/bin/backup.sh
RUN chmod +x /usr/local/bin/backup.sh
ENTRYPOINT ["/usr/local/bin/backup.sh"]
CMD ["loop"]
