# spectrum-slack-buf

Canonical Protobuf schemas for the `photon.slack.v1` runtime contract.

This repository is the single source of truth for the gRPC wire protocol
between [`spectrum-slack`](https://github.com/photon-hq/spectrum-slack)
(the server) and per-language SDKs like
[`slack-ts`](https://github.com/photon-hq/slack-ts).

## Layout

```
photon/slack/v1/
├── common.proto            Inbound event variants, cursors, heartbeats, file metadata
├── message_service.proto   MessageService (SendMessage, MarkRead, SubscribeEvents, FetchMissedEvents)
└── file_service.proto      FileService (Upload, GetUrl)
```

## Consumers

Both the server and each SDK pull this repo in as a git submodule tracking
the `main` branch:

```sh
git submodule add -b main https://github.com/photon-hq/spectrum-slack-buf.git proto
```

SDKs run `buf generate proto` to emit language bindings, and CI re-runs
`git submodule update --remote --merge proto` on every build so a breaking
change here surfaces immediately downstream.

## Working on the schema

```sh
# Verify the protos lint cleanly.
buf lint

# Verify changes don't break the wire format against the last release.
buf breaking --against '.git#branch=main'
```

Breaking changes are not allowed on `main`. To make one, bump the package
to `photon.slack.v2` and ship the new service in parallel.

## License

MIT — see [LICENSE](LICENSE).
