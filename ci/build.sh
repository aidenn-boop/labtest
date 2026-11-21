#!/bin/bash
# Build our bot and the sandboxed rival field exactly as in the local lab (same commits, same images).
set -e
R=$PWD
G="golang:1.26.1-bookworm@sha256:ab3d6955bbc813a0f3fdf220c1d817dd89c0b3f283777db8ece4a32fe7858edd"
docker run --rm -u $(id -u):$(id -g) -v "$R/bot":/src -w /src -e CGO_ENABLED=0 -e GOOS=linux -e GOARCH=amd64 \
  -e GOCACHE=/tmp/gocache -e HOME=/tmp $G go build -trimpath -ldflags="-s -w" -o /src/mechbot ./cmd/bot
mkdir -p bin && mv bot/mechbot bin/mechbot
docker build -q -t field-giddy rivals/giddygiddyboy >/dev/null
clone() { git clone -q "https://github.com/$1" "/tmp/$2" && git -C "/tmp/$2" checkout -q "$3" && docker build -q -t "field-$2" "/tmp/$2" >/dev/null; }
clone devakchow/team-kanyaraasi-mech-royale kanya 6a17c499d6b881ee4eca0051a4bf5c9fae5ce6c2
clone JuanQuenga/buildathon-mech-royale jq 48288e2b84f8c96685fdd98f09b4960bf1c51876
clone JishnuMadav/mech-royale jishnu d53c5daf90caaf5d1945868f597db247aa121e61
A="$R/arena"
SB="docker run -i --rm --network none --cpus 1 --memory 512m --user 65534:65534 --read-only --tmpfs /tmp -v $A:$A:ro"
mkdir -p field
echo "bot*1,cmd:$SB field-giddy*3,cmd:$SB field-kanya*3,cmd:$SB field-jq*3,cmd:$SB field-jishnu*3,builtin:expert*3" > field/lineup_A
docker images --format '{{.Repository}} {{.Size}}' | grep field-
