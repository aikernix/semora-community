FROM golang:1.26-alpine AS build
WORKDIR /src
COPY services/ontology/go.mod services/ontology/go.sum ./
ARG GOPROXY=https://proxy.golang.org,direct
ENV GOPROXY=$GOPROXY
RUN go mod download
COPY services/ontology/cmd ./cmd
RUN --mount=type=cache,target=/root/.cache/go-build go test ./cmd/server && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /ontology ./cmd/server

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /ontology /ontology
EXPOSE 8081
ENTRYPOINT ["/ontology"]

