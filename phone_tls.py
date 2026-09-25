"""The HTTPS setup for the phone server: one port, the right certificate for each caller.

JARVIS can serve two certificates, and each caller needs a different one:

- the phone, reaching JARVIS by its Tailscale name, wants the certificate
  issued for that name (JARVIS_CERT / JARVIS_KEY, from Let's Encrypt via
  Tailscale), which it trusts with nothing installed;
- a sensor board on the wifi connects by address and checks against
  JARVIS's own authority (setCACert), so it needs the local certificate,
  which names this PC's addresses. A certificate issued for a name can
  never satisfy it.

They are told apart by what the caller asks for. A browser using a name
says so as it connects (SNI); a board connecting by address names nothing
(an address is never sent as a name; one that sends it anyway is treated
the same). So a caller that names a host gets the issued certificate, and
one that does not gets the local one -- same port, same token, same
everything else.

Kept apart from phone.py so it can be tested without starting the voice
system, which importing phone.py does.
"""

import ipaddress
import ssl


def _is_address(name):
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def wants_local(server_name):
    """Whether a caller asking for [server_name] should get the local certificate."""
    return not server_name or _is_address(server_name)


def serving_context(cert, key, local=None):
    """TLS for the phone server.

    [cert] and [key] are what it serves by default. With [local] -- the
    (cert, key) of JARVIS's own certificate -- a caller that names no
    host is given that instead.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)

    if local:
        by_address = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        by_address.load_cert_chain(*local)

        def choose(connection, server_name, _context):
            if wants_local(server_name):
                connection.context = by_address

        context.sni_callback = choose

    return context
