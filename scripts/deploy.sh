#!/usr/bin/env bash

cd ..

# Before running, setup project
# gcloud init
# gcloud auth login

PROJECT="<Project Here>"

echo "Setting project: $PROJECT"
gcloud config set project "$PROJECT"
gcloud config set compute/zone "us-central1-a"

echo "Building app container"
docker build -t "gcr.io/$PROJECT/app" .

echo "Pushing to container registry"
gcloud docker -- push "gcr.io/$PROJECT/app"

echo ""
echo "Latest version deployed to Container Registry"
echo "https://console.cloud.google.com/gcr/images/$PROJECT"
echo ""

echo "Manual Step:"
echo "Updates are not automatic during alpha"
echo "Please delete all VM instances here:"
echo "https://console.cloud.google.com/compute/instances?project=$PROJECT"
echo "Once deleted, they will be recreated with latest version"
