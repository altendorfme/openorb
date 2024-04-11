FROM ubuntu:latest
RUN apt-get update && apt-get -y install cron curl
COPY crawl-cron /etc/cron.d/crawl-cron
RUN chmod 0644 /etc/cron.d/crawl-cron
RUN crontab /etc/cron.d/crawl-cron
RUN touch /var/log/cron.log
CMD cron && tail -f /var/log/cron.log