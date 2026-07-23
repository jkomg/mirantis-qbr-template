# Mirantis QBR Template — runtime image
# -----------------------------------------------------------
# Two-stage build:
#   1. fetch-fonts stage downloads the Google Fonts woff2 files
#      using curl. This is the ONLY network step.
#   2. runtime stage is a tiny nginx:alpine serving the static
#      files. Once built, the container makes zero external
#      requests — safe for handling real customer data.
# -----------------------------------------------------------

# ---- Stage 1 · fetch fonts ----
FROM alpine:3.20 AS fonts
RUN apk add --no-cache curl bash
WORKDIR /work
COPY scripts/fetch-fonts.sh /work/scripts/fetch-fonts.sh
RUN bash /work/scripts/fetch-fonts.sh /work/assets/fonts

# ---- Stage 2 · runtime ----
FROM nginx:alpine

# Drop the default nginx site
RUN rm -rf /usr/share/nginx/html/*

# Copy the deck + configurator + data + assets
COPY ["QBR Configurator.dc.html",         "/usr/share/nginx/html/"]
COPY ["QBR Template.dc.html",             "/usr/share/nginx/html/"]
COPY ["perf-report.html",                 "/usr/share/nginx/html/"]
COPY ["QBR Template - 3 Directions.dc.html", "/usr/share/nginx/html/"]
COPY ["deck-stage.js",                    "/usr/share/nginx/html/"]
COPY ["support.js",                       "/usr/share/nginx/html/"]
COPY ["qbr.data.json",                    "/usr/share/nginx/html/"]
COPY ["assets/",                          "/usr/share/nginx/html/assets/"]
COPY ["AUTOMATION.md",                    "/usr/share/nginx/html/"]
COPY ["SERVICE-CONTRACT.md",              "/usr/share/nginx/html/"]
COPY ["LOCAL-SETUP.md",                   "/usr/share/nginx/html/"]
COPY ["SALESFORCE-OAUTH-SETUP.md",        "/usr/share/nginx/html/"]
COPY ["scripts/mirantis-qbr-sync.js",     "/usr/share/nginx/html/scripts/mirantis-qbr-sync.js"]

# Copy the locally-fetched fonts on top of assets/
COPY --from=fonts /work/assets/fonts /usr/share/nginx/html/assets/fonts

# Small landing page + docs viewer so the bare URL doesn't 404
COPY index.html         /usr/share/nginx/html/index.html
COPY docker/docs.html   /usr/share/nginx/html/docs.html

# Tighten nginx for a private/local context: no upstream connections,
# directory listing on, sensible MIME types, big-payload tolerant for JSON.
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 8080
HEALTHCHECK CMD wget -q --spider http://localhost:8080/ || exit 1
