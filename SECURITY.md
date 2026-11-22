# Security policy

Bling Reborn holds a sign-in to your Blink account (an OAuth refresh token, encrypted with Windows DPAPI) and
downloads your camera footage, so security problems matter. Thank you for reporting them responsibly.

## Reporting a vulnerability

**Please don't open a public issue.** Use GitHub's private reporting instead: open the **Security** tab of
this repository and choose **Report a vulnerability**. Only the maintainer can see the report.

Please include what the problem is, what an attacker could do with it, the Bling Reborn version and steps to
reproduce. Leave out your own account details, tokens and clip links.

## Supported versions

Only the latest release is supported.

## In scope

- How the sign-in is stored, refreshed and protected.
- Anything that could leak footage, tokens or clip links, or let another local user or program read them.
- The installer and update path.

Blink's own service and apps are out of scope; report those to Blink.
