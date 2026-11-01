# codee-teams

A [Codee](https://github.com/fusebase-dev/codee) plugin that runs a skill when a message
is posted in the Microsoft Teams channels it watches.

## Install

In your Codee project:

```bash
uv add codee-teams
```

Restart Codee. **Microsoft Teams** appears on the **Plugins** page.

## Set up

1. **Register an Entra app** (Entra admin center → App registrations → New registration).
   - Under *Authentication*, add a **Web** redirect URI: copy it from the plugin's card
     on the Plugins page (`<admin URL>/api/oauth/plugins/teams/callback`).
   - Under *Certificates & secrets*, create a client secret.
   - Under *API permissions*, add these **delegated** Microsoft Graph permissions, then
     **Grant admin consent**: `ChannelMessage.Read.All`, `User.ReadBasic.All`,
     `User.Read`, `offline_access`, `Team.ReadBasic.All`, `Channel.ReadBasic.All`.
2. On the Plugins page, fill in the tenant ID, client ID and secret, then **Sign in**.
   Sign in with the account Codee should read Teams as. It only sees the teams that
   account is a member of.
3. List any accounts whose messages should never fire a skill under **Ignore messages
   from**, e.g. the account the agent posts its replies as. The signed-in account is
   always ignored.

## A skill

On the Skills page, pick the skill type **teams-message trigger**, click **Teams channels**
and search by team or channel name; pick as many channels as the skill should watch. They
are shown by name; the skill file stores each as `<team id>/<channel id>  # Team / Channel`,
the part after ` #` being only a label.

```yaml
---
name: teams-support
description: Answer questions posted in the support channel.
disable-model-invocation: true
x-codee-trigger: teams-message
x-teams-channels: |-
  0f3c...-team-id/19:a1b2...@thread.tacv2  # Support / General
---

Someone wrote in Teams channel {TEAMS_CHANNEL_ID}:

{TEAMS_MESSAGES}

Answer the message marked "triggered this run".
```

The editor shows the full list of placeholders beside the skill body.

## Only some topics

Set **Messages** on the skill to **Specific topics** and list the topics it handles, one
per line. Each new message is then first put to a decision model, and the skill runs only
when the model says the message is about one of them. The question is about the message
that fired the run, read as part of its conversation: a reply that continues a discussion
about a topic (a follow-up question, a doubt about an answer) counts even when it doesn't
name the topic; a message that turns to something else doesn't. The matched topic
is in `{TEAMS_TOPIC}`.

Pick the model under **Decision model** on the Plugins page:

- **OpenAI**: an API key; the model is `gpt-6-luna`, the only one OpenAI's
  [Decisions API](https://developers.openai.com/api/docs/guides/decisions) takes for now.
  Each message is one *choice* question: which of the skill's topics it is about, or
  "none of these". A refused question counts as no match.
- **TypeSafe (Jev)**: an API key from console.typesafe.ai/keys and a model (`jev-latest`,
  or a version such as `jev-1.14.0`). TypeSafe answers each topic with the probability
  that the message is about it; the **Match threshold** (0.7 by default) decides.

A message the model says no to is done; it isn't asked about again. If the model can't be
reached, the message is tried again on the next ticks, three times in all. At most 20
decisions are asked per tick.

## How it works

- Every 30 seconds, the plugin reads the 50 most recently active threads of
  each watched channel, with their replies, and runs the skill once per message it hasn't
  handled yet.
- **One message per channel at a time.** A run happens in the background, so the rest of
  Codee keeps working. While a channel has a run going, its new messages wait (not even
  sent to the decision model) and are handled one per tick, oldest first, once it ends;
  the next one is then judged with the run's own reply in the thread. Different channels
  run side by side, at most 3 new runs started per tick.
- Messages posted before the skill was saved with a channel don't fire it. Anything
  posted after that does, including messages that arrived while Codee was paused or
  restarting: they run on the next tick.
- A reply gets the whole thread; a top-level message gets the 10 top-level messages before
  it. Each message comes with its ID, date and sender (name, email, user ID).
- A run that fails is retried on the next ticks, three times in all. A run killed from the
  dashboard is not retried.
- What has been handled is kept in `.codee/plugins/teams/state.json`, the tokens in
  `.codee/plugins/teams/token.json`.

## Limits

- A reply on a thread that isn't among the channel's 50 most recently active threads is
  not seen. Graph orders threads by their latest change, so a new reply normally brings
  its thread to the top.
- Messages older than 14 days never fire a skill.
- Reading channel messages through Graph counts as a protected API; check your tenant's
  policy before relying on it.
