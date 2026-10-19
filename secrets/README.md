# secrets/

Put your Vonage application's private key here as `private.key`.

```bash
cp ~/Downloads/private.key secrets/private.key
chmod 700 secrets              # other users on this machine can't open the folder
chmod 644 secrets/private.key  # the container's unprivileged user can read the file
```

Everything in this folder except this README is ignored by Git and excluded from
the Docker build context. `compose.yaml` mounts the key read-only into the
container at `/run/secrets/vonage_private_key`.
