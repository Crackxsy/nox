"""Network access: the dashboard for paired devices in the home network.

`tls` makes the certificates, `devices` pairs and recognises devices, `app` is the HTTPS listener
and `install` wires it into the core. Off unless `ipc.lan.enabled` is set.
"""
