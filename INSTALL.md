# How to install Catalyst

You do not need to be technical. There is one command to run, and then
a web page to fill in. Nothing needs editing by hand.

## What you need before starting

1. **An Ubuntu server** (a basic VPS from any provider is fine).
2. **Your Alpaca practice-account keys** — from alpaca.markets, under
   "Paper Trading". Two values: a key and a secret.
3. **Your Anthropic API key** — from console.anthropic.com.
4. *(Recommended)* **Your Anthropic ADMIN key** — also from
   console.anthropic.com, under Organization, then API keys. This one
   lets Catalyst check its own maths against your real Anthropic bill
   every night. It is only ever used to READ the bill — never to change
   any limit or setting.

Don't worry about copying these down in advance — the setup page tells
you exactly where to find each one, and has a Test button beside each
so you can check a key works before saving it.

## Step 1 — get the code onto the server

Your repository is **private**, so the server needs permission to
download it. Pick whichever of these you prefer — then log in to your
server and run the commands for that option.

**Option A — make the repository public** (simplest)

On github.com open your `catalyst` repository, go to Settings, scroll to
the bottom, and choose "Change visibility" → Public.

This is safe: **no keys or passwords are stored in the repository.** Your
Alpaca and Anthropic keys live only on your own server, in a file only
Catalyst can read. That is deliberate — it is why the setup page exists.
Making it public means anyone can read the code, not your keys or your
trading.

Then on the server:

```
git clone https://github.com/JustAParrot11/catalyst.git
cd catalyst
```

**Option B — keep it private** (use a one-time access token)

On github.com click your profile picture (top right) → Settings →
Developer settings → Personal access tokens → **Fine-grained tokens** →
Generate new token. Give it any name, set "Repository access" to *Only
select repositories* and pick `catalyst`, then under Permissions →
Repository permissions set **Contents: Read-only**. Generate it and copy
the token — it is shown only once.

Then on the server, pasting your token where shown:

```
git clone https://YOUR-TOKEN@github.com/JustAParrot11/catalyst.git
cd catalyst
```

That token is only for downloading the code. It is not one of the keys
the setup page asks for later.

## Step 2 — run the installer

```
sudo bash install/install.sh
```

This installs everything, starts Catalyst as a background service, and
runs its own checks as it goes. If a step fails it stops and tells you
exactly what went wrong and what to do about it. It is safe to run
again — re-running never deletes anything.

## Step 3 — open the link it prints

When the installer finishes it prints a link like:

```
http://YOUR-SERVER-ADDRESS:8000/?code=...
```

**This is where your keys are asked for.** The installer never asks for
them in the terminal, and you never put them in a file. The page shows a
box for each key, explains where to find it, and has a Test button
beside it that tells you straight away whether the key works. You also
choose practice or real money here, and your monthly research budget.

When you press Save, the keys are written to a file on your own server
that only Catalyst can read, and the bot picks them up from there every
time it runs. They are never shown on screen again, never written to the
logs, and never included in anything you might share. Takes about five
minutes, and you never do it again.

**Keep that link private** — the code at the end of it is what keeps
other people out of your bot.

That's it. Catalyst starts trading with practice money and the same
page becomes its dashboard: what it's looking at, what it decided and
why, and exactly what it is spending.

## Later on

| You want to | Run this on the server |
|---|---|
| Check it is running | `sudo systemctl status catalyst` |
| Watch what it is doing | `sudo journalctl -u catalyst -f` |
| Update to a newer version | `sudo bash install/upgrade.sh` |

The upgrade command backs up the database first, runs the full test
suite after, and puts everything back the way it was if anything fails.

## If the page will not open

Catalyst writes a reason to its log every time. Read it with:

```
sudo journalctl -u catalyst -n 50 --no-pager
```

**"Could not open the setup page on 0.0.0.0:8000"** — something else on
the server is already using port 8000. The bot itself is fine and still
trading; only the web page is affected. Find the culprit:

```
sudo ss -ltnp | grep :8000
```

If the answer mentions `catalyst`, you have a second copy running — stop
the extra one, or just reboot the server; the copy started by
`systemctl` is the one to keep. If it is some other program, either stop
that program or move Catalyst to another port:

```
sudo systemctl edit catalyst
```

Add these three lines, save, then restart:

```
[Service]
Environment=CATALYST_PORT=8001
```

```
sudo systemctl restart catalyst
```

**"Catalyst is already running on this machine"** — that is not an
error. A second copy noticed the first and stood down on purpose, so
that two bots could never place the same order twice. Nothing to do.

**The page times out with no error in the log** — the server is
answering but your provider's firewall is blocking the port. Their
control panel will have a firewall page where you can allow port 8000
for your own address.

## Two things worth knowing

- **It starts on practice money and stays there** until you make a
  deliberate choice on the setup page to switch. It will never move to
  real money by itself.
- **The link not opening** almost always means your server provider's
  firewall is blocking port 8000 — their control panel has a page where
  you can allow it for your own address.
