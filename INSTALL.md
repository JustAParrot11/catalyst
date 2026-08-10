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

Log in to your server and run:

```
git clone https://github.com/JustAParrot11/catalyst.git
cd catalyst
```

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

Open it in your browser. The page asks for the keys from the list
above, explains each one, and has a Test button beside each. Takes
about five minutes, and you never do it again.

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

## Two things worth knowing

- **It starts on practice money and stays there** until you make a
  deliberate choice on the setup page to switch. It will never move to
  real money by itself.
- **The link not opening** almost always means your server provider's
  firewall is blocking port 8000 — their control panel has a page where
  you can allow it for your own address.
