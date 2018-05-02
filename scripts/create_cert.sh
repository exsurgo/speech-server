#!/usr/bin/env bash

# Create a self-signed certificate to run with localhost or an IP address

echo "Enter a file name: "
read name

echo "Creating cert"
openssl req -x509 -sha256 -nodes -newkey rsa:2048 -days 10000 -keyout "$name.key" -out "$name.crt"

# Adds the cert to your local keychain to prevent browser errors
# Note: This should only work in OSX.  Will need to modify for Windows/Linux

echo "Adding cert to local keychain"
open "$name.crt"
sudo security add-trusted-cert -d -r trustRoot -k "/Library/Keychains/System.keychain" "$name.crt"

