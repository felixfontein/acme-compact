# acme-compact

This is a compact, modularized Python ACME library and command line interface
which allows to issue and renew [Let's Encrypt](https://letsencrypt.org/)
certificates for your server, by either running this script on your server or
by running it somewhere else. It does needs access to your private Let's Encrypt
account key. Please note that this code is somewhat experimental, so don't use
this in production environments without checking the code first. Since there
isn't too much code (around 610 lines, including docstrings and CLI help) this
should be manageable.

**PLEASE READ THE SOURCE CODE! YOU MUST TRUST IT WITH YOUR PRIVATE KEYS!**

The code is based on acme-tiny by Daniel Roesler, which can be found at
https://github.com/diafygi/acme-tiny. I used a Python 3 adjusted version by
Collin Anderson as a base (available at https://github.com/collinanderson/acme-tiny
as branch py3).

The main reason why I created this version is that it is more modular; in
particular, it allows to run this script on another machine than the webserver!
Also, it can be easily integrated into scripts and orchestration programs such
as ansible to handle certificate creation on another machine. While at it, I also
added commands for some more steps (account key creation, private key and CSR
creation) so everything is bundled in one place.

This modularization comes at a cost: it increases the code size a bit. That's
why I started a new project instead of simply forking (and starting a Pull
Request for) the original project.

This code should work with Python 2 (untested) and Python 3, and requires OpenSSL's
command line tool openssl in the path.
