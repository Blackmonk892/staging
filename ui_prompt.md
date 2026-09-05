I want you to add a SIMPLE, CENTRALIZED WEB UI to the staging cloud.

This is now the intended operator/customer onboarding flow:

```text
                 STAGING CLOUD
              https://<render-url>
                       │
                       ▼
                 CENTRAL UI
                       │
          ┌────────────┴────────────┐
          │                         │
   Create Pairing Code        Agent Status
          │
          ▼
     CODE: ABC123
          │
          │ operator gives code
          ▼
      CUSTOMER LAPTOP
          │
          ▼
      NZeroC Agent UI
          │
          │ enter ABC123
          ▼
       Enrollment
          │
          ▼
     agent credential
          │
          ▼
    heartbeat / config sync
          │
          ▼
                 STAGING CLOUD
                       │
                       ▼
                Agent appears
                as connected
```

I do NOT want the operator to use curl, Postman, API calls, or manually construct URLs.

## IMPORTANT

Work only inside the standalone staging-cloud repository.

Do NOT modify the NZeroC agent, devcloud, vision pipeline, MSI/WiX, or production code.

Do NOT redesign the existing APIs.

The existing pairing/enrollment APIs already work and have been tested. Build the UI ON TOP of those APIs.

---

# 1. CENTRAL ADMIN UI

Create a central browser UI at:

```text
/admin
```

This should be the main landing page for the staging cloud.

Protect it using the existing `STAGING_ADMIN_TOKEN` authentication mechanism.

I want a very simple clean UI, not a giant frontend framework.

Server-rendered HTML is perfectly fine.

The page should have:

### Header

```text
NZeroC
Staging Cloud
```

and clearly indicate:

```text
STAGING
```

### Main sections

#### A. Pair a New Agent

Show:

```text
Pair a New Agent

[ Generate Pairing Code ]

Your pairing code:
ABC123

Give this code to the user installing the
NZeroC Agent on their laptop.

Code expires in: XX:XX
```

The code should be generated using the EXISTING pairing-code backend/API.

Do not invent a second pairing mechanism.

After clicking Generate:

* display the code prominently
* show its expiry
* allow generating another code
* do not expose database credentials
* do not expose agent credentials

If the existing pairing-code API requires the admin token, use the authenticated server-side path appropriately.

---

# 2. AGENT LIST / STATUS

Below the pairing section, show:

```text
Agents

┌─────────────────────────────────────────────┐
│ Agent       Site       Status      Last Seen│
│ laptop-01   test-site  Connected   10 sec   │
│ laptop-02   site-2     Offline     4 min    │
└─────────────────────────────────────────────┘
```

Use the existing agent repository/API data.

At minimum show:

* agent ID
* site ID
* status
* last heartbeat
* credential status if already available safely

Do NOT display raw credentials.

Add a refresh mechanism.

Polling every ~5-10 seconds is fine.

---

# 3. ENROLLMENT FLOW MUST BE CLEAR

The UI should explain exactly what the operator does:

```text
1. Generate a pairing code here.
2. Give the code to the person installing the NZeroC Agent.
3. They enter the code in the NZeroC Agent UI.
4. The agent enrolls automatically.
5. This page shows the agent when it connects.
```

This is the UX we want.

The operator should NOT need to understand:

* `/admin/pairing-codes`
* `/v1/agents/enroll`
* bearer tokens
* MongoDB
* HTTP requests
* API payloads

Those are implementation details.

---

# 4. VIDEO SHOULD ALSO BE CENTRALIZED

Do not make `/video` feel like an unrelated separate product.

The central admin UI should have navigation:

```text
NZeroC Staging

[ Dashboard ] [ Pair Agent ] [ Agents ] [ Cameras / Video ]
```

You can either make these sections on `/admin` or have simple links to:

```text
/admin
/video
```

The goal is that the operator has ONE obvious starting point:

```text
/admin
```

From there they can:

* pair an agent
* see connected agents
* see cameras/streams
* open live video

Keep the existing `/video` implementation and HLS behavior.

Do not rewrite the video backend.

---

# 5. AFTER AGENT PAIRS

Once the user enters the pairing code in the NZeroC Agent UI:

```text
pairing code
      ↓
POST /v1/agents/enroll
      ↓
credential issued
      ↓
credential persisted by agent
      ↓
heartbeat
      ↓
config sync
      ↓
admin dashboard shows Connected
```

Make sure the UI reflects this naturally.

For example:

```text
Pairing code: ABC123

Waiting for agent...

● Waiting for enrollment

then:

✓ Agent enrolled

Agent: laptop-01
Site: test-site
Status: Connected
Last heartbeat: 3 seconds ago
```

Do not fake the status. It must come from the actual backend state.

---

# 6. TOKEN HANDLING

The browser UI must use the existing admin authentication.

Do NOT put `STAGING_ADMIN_TOKEN` into the repository.

Do NOT render the token itself anywhere.

Do NOT expose agent bearer credentials.

Do NOT weaken authentication just to make the UI easier.

If the current `?token=` approach is the existing mechanism for `/video`, preserve compatibility, but make the admin experience as clean as possible.

If you can implement a simple authenticated admin session/cookie without changing the API security model, that is preferable.

However, do NOT introduce unnecessary authentication architecture just for this task.

---

# 7. RENDER COMPATIBILITY

This must work on the existing Render deployment:

https://nzerox-staging-cloud.onrender.com

The UI must work with:

* Render Free
* existing MongoDB Atlas
* existing environment variables
* existing aiohttp server
* existing deployment configuration

No Node build pipeline is required.

Prefer the existing server-side HTML approach.

---

# 8. KEEP THE EXISTING API CONTRACTS

Do NOT change:

```text
POST /admin/pairing-codes
POST /v1/agents/enroll
POST /v1/agents/{id}/heartbeat
GET  /v1/agents/{id}/config
POST /v1/ingest/video/{site_id}/{camera_id}
```

The UI is just an operator layer over the existing backend.

---

# 9. TEST THE REAL FLOW

Add tests for the UI.

Most importantly, test:

```text
Admin opens /admin
        ↓
authenticated
        ↓
Generate Pairing Code
        ↓
real pairing code created
        ↓
agent enrollment using that code
        ↓
agent appears in dashboard
        ↓
heartbeat updates last-seen/status
```

Do not merely test that an HTML button exists.

Test the actual backend interaction.

Also verify the existing video UI still works.

Run:

* pytest
* ruff
* mypy

---

# 10. FINAL UX

When I open:

```text
https://<render-url>/admin
```

I should immediately understand:

```text
NZeroC STAGING

Pair a New Agent

[ GENERATE PAIRING CODE ]

        ABC123

Give this code to the user installing
NZeroC Agent on their laptop.

        ↓

Agents

Connected Agents
...
```

This should become the SINGLE CENTRAL place I use when setting up a laptop.

I should NOT need to use curl or manually call an API.

---

# FINAL REPORT

Tell me:

1. Files changed.
2. What UI routes were added.
3. How authentication works.
4. How pairing-code generation is connected to the existing backend.
5. How the agent enrollment flow works end-to-end.
6. How agent status is populated.
7. Tests added and results.
8. Confirm no changes were made to the NZeroC agent.
9. Confirm no secrets were committed.

Most importantly:

DO NOT stop at "UI added".

Actually verify the flow:

```text
/admin
→ generate pairing code
→ use code for real enrollment
→ heartbeat
→ agent appears in admin UI
```

The objective is to turn the current API-driven staging cloud into a simple centralized operator experience.
