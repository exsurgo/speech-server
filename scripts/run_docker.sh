#!/usr/bin/env bash

cd ..

echo "Stopping/Removing all containers"
docker stop "$(docker ps -a -q)"
docker rm "$(docker ps -a -q)"

echo "Building/Running app container"
docker build -t streaming-speech-service .
docker run -p 3080:80 -p 3443:443 -t streaming-speech-service
