# networkd: one static-enough binary on a distroless base. Built from
# this directory (the Cargo workspace).
FROM rust:1-bookworm AS build
WORKDIR /src
COPY . .
RUN cargo build --release --locked -p networkd

FROM gcr.io/distroless/cc-debian12:nonroot
# the Python image's working directory, so the data/ paths both services
# default to (legs.db, boards.db, refdata.sqlite) resolve alike
WORKDIR /srv/api
COPY --from=build /src/target/release/networkd /usr/local/bin/networkd
EXPOSE 8092
HEALTHCHECK --interval=10s --timeout=5s --retries=5 CMD ["/usr/local/bin/networkd", "--health"]
ENTRYPOINT ["/usr/local/bin/networkd"]
