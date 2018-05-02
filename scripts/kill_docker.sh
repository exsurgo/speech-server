#!/usr/bin/env bash

cd ..

echo "Stopping/Removing all containers"
docker stop "$(docker ps -a -q)"
docker rm "$(docker ps -a -q)"
