# @photon-ai/slack

TypeScript SDK for Slack, via [Spectrum](https://github.com/photon-ai).

This is a thin client over the `spectrum-slack` gRPC runtime. You provide
the JWTs (one per workspace); the SDK handles transport, retries, and
event streaming.

## Install

```sh
bun add @photon-ai/slack
```

## Quick start

```ts
import {
  createClient,
  staticTokens,
  text,
  blocks,
  section,
  divider,
} from "@photon-ai/slack";

const client = createClient({
  tokenProvider: staticTokens({
    tokens: { T012ABCDE: process.env.SLACK_JWT! },
    teams: {
      T012ABCDE: {
        teamName: "Acme",
        botUserId: "U-BOT",
        appId: "A-APP",
        grantedScopes: ["chat:write"],
      },
    },
  }),
});

// Pick a team and act on it.
const team = client.team("T012ABCDE");

// Send text.
await team.messages.send({ channel: "C012XYZ", ...text("hello") });

// Send Block Kit blocks.
await team.messages.send({
  channel: "C012XYZ",
  ...blocks(
    [section("Hello *world*"), divider()],
    "Hello world (fallback)"
  ),
});

// Reply in a thread.
await team.messages.send({
  channel: "C012XYZ",
  threadTs: "1700000000.000100",
  ...text("threaded reply"),
});

// React to a message.
await team.messages.send({
  channel: "C012XYZ", // ignored for reactions
  reaction: {
    emoji: "thumbsup",
    itemTs: "1700000000.000100",
    itemChannel: "C012XYZ",
  },
});
```

## Subscribe to events

```ts
const stream = client.team("T012ABCDE").events.subscribe();

for await (const event of stream) {
  switch (event.type) {
    case "message":
      console.log(`${event.message.user}: ${event.message.text}`);
      break;
    case "mention":
      console.log(`@bot mentioned by ${event.mention.user}`);
      break;
    case "reaction":
      console.log(`reaction ${event.reaction.name} (removed=${event.reaction.removed})`);
      break;
    case "interactive":
      console.log("interactive:", event.interactive.rawPayload);
      break;
    case "command":
      console.log(`/${event.command.command} ${event.command.text}`);
      break;
  }
}
```

The stream reconnects automatically on `UNAVAILABLE`, draining missed events
via `fetchMissedEvents` before resuming live. It exits cleanly when the
platform is disabled (`PermissionError(kind: "platform_disabled")`).

To survive process restarts, plug in a persistent cursor store:

```ts
const client = createClient({
  tokenProvider: staticTokens({ tokens, teams }),
  cursorStore: {
    async get(teamId) {
      return await redis.get(`slack:cursor:${teamId}`) ?? undefined;
    },
    async set(teamId, cursor) {
      await redis.set(`slack:cursor:${teamId}`, cursor);
    },
  },
});
```

## Upload a file

```ts
const { file, shares } = await client.team("T012ABCDE").files.upload({
  channel: "C012XYZ",
  filename: "report.pdf",
  mimeType: "application/pdf",
  content: await fs.readFile("./report.pdf"),
  initialComment: "Here's the report",
});

// `shares` has one entry per channel id passed in `channel` — use
// (channel, ts) to react/reply/edit/delete the share message Slack
// created when the file was posted.
const share = shares[0];
if (share) {
  await client.team("T012ABCDE").messages.send({
    channel: share.channel,
    reaction: {
      emoji: "thumbsup",
      itemTs: share.ts,
      itemChannel: share.channel,
    },
  });
}
```

## Errors

All errors are subclasses of `SlackError`. Use `instanceof` to handle them:

```ts
import {
  AuthenticationError,
  ConnectionError,
  NotFoundError,
  PermissionError,
  RateLimitError,
  SlackError,
  ValidationError,
} from "@photon-ai/slack";

try {
  await team.messages.send({ channel: "C1", ...text("hi") });
} catch (err) {
  if (err instanceof PermissionError && err.permission.kind === "feature_not_enabled") {
    console.error(`feature disabled: ${err.permission.feature}`);
  } else if (err instanceof RateLimitError) {
    console.error(`rate limited; retry after ${err.retryAfterMs}ms`);
  } else if (err instanceof SlackError) {
    console.error(err);
  }
}
```

`PermissionError.permission` is a typed discriminated union covering
`feature_not_enabled`, `platform_disabled`, `team_not_owned`, and `other`.

Feature-gated RPCs today: `files.upload` / `files.getUrl` (feature `files`),
reactions via `messages.send({ reaction })` (feature `reactions`), and
`messages.markRead` (feature `read-tracking`).

## Token providers

`createClient` requires a `tokenProvider` — the SDK consumes JWTs through
the `TokenProvider` contract and stays agnostic about where they come from.
`staticTokens` ships in the box: a fixed `team_id → JWT` map plus team
metadata. Mint or refresh tokens however you like out-of-band, then plug
the result into `staticTokens` (or implement `TokenProvider` directly).

```ts
import { createClient, staticTokens } from "@photon-ai/slack";

const client = createClient({
  tokenProvider: staticTokens({
    tokens: { T012ABCDE: "eyJ..." },
    teams: {
      T012ABCDE: {
        teamName: "Acme",
        botUserId: "U-BOT",
        appId: "A-APP",
        grantedScopes: ["chat:write"],
      },
    },
  }),
});
```

## Configuration

| Env var | Default |
|---|---|
| `SPECTRUM_SLACK_ENDPOINT` | `slack-grpc.spectrum.photon.codes:443` (overridable via the `spectrumSlackEndpoint` option on `createClient`) |

For local dev, set it to `localhost:50051` — the SDK uses insecure gRPC
for `localhost:` addresses.

## Development

The `.proto` files live in
[`photon-hq/spectrum-slack-buf`](https://github.com/photon-hq/spectrum-slack-buf)
and are mounted as a git submodule at `./proto/`, tracking its `main` branch.

```sh
git clone --recurse-submodules <slack-ts-url>
# or, if already cloned:
git submodule update --init --merge proto

bun install
bun run generate        # buf generate proto → src/generated
bun run check           # tsc --noEmit
bun test
bun run build           # tsdown → dist/

# Pull the latest commit from the proto repo's main into the working tree:
bun run proto:update
```

CI always runs `git submodule update --remote --merge proto` before
generating, so every build uses the live tip of the proto repo's `main`.
The committed submodule SHA is a developer convenience, not a pin —
a breaking proto change shows up on the next CI run.
