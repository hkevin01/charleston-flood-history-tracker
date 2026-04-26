FROM nginx:alpine

WORKDIR /usr/share/nginx/html
RUN rm -f index.html

COPY index.html ./index.html
COPY src/ ./src/
COPY data/processed/ ./data/processed/
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
HEALTHCHECK --interval=15s --timeout=3s CMD curl -sf http://localhost/ > /dev/null || exit 1
CMD ["nginx", "-g", "daemon off;"]
