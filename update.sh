#!/usr/bin/env bash

sudo dnf upgrade -y
flatpak update -y
sudo snap refresh
sudo npm -g update
pnpm -g update
bun -g update
rustup update
cargo update
