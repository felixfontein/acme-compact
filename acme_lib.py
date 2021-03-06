"""
Compact ACME library.

Based on the py3 branch of https://github.com/collinanderson/acme-tiny,
which is based on https://github.com/diafygi/acme-tiny.

This code is licensed under the MIT license; see LICENSE for details.

The original acme-tiny code is Copyright (c) 2015 Daniel Roesler
"""

import base64
import binascii
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import textwrap
import time
try:
    from urllib.request import urlopen, Request
except ImportError:  # Python 2
    from urllib2 import urlopen, Request


staging_ca = "https://acme-staging.api.letsencrypt.org"
default_ca = "https://acme-v01.api.letsencrypt.org"
default_ca_staging = "https://acme-staging.api.letsencrypt.org"
default_intermediate_url = "https://letsencrypt.org/certs/lets-encrypt-x3-cross-signed.pem"
default_root_url = "https://letsencrypt.org/certs/isrgrootx1.pem"
ca_agreement = "https://letsencrypt.org/documents/LE-SA-v1.1.1-August-1-2016.pdf"
ca_agreement_redirect_pattern = '{}/terms'

user_agent = "acme-compact"

# #####################################################################################################
# # Helper functions


def _b64(b):
    """Helper function base64 encode for jose spec."""
    return base64.urlsafe_b64encode(b).decode('utf8').replace("=", "")


def _run_openssl(args, input=None):
    """Execute OpenSSL with the given arguments. Feeds input via stdin if given."""
    if input is None:
        proc = subprocess.Popen(["openssl"] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = proc.communicate()
    else:
        proc = subprocess.Popen(["openssl"] + list(args), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = proc.communicate(input)
    if proc.returncode != 0:
        raise IOError("OpenSSL Error: {0}".format(err.decode('utf-8')))
    return out


def _get_wellknown_path(domain, token, folder_for_domain):
    """Retrieve path for token file."""
    if callable(folder_for_domain):
        folder = folder_for_domain(domain)
    else:
        folder = folder_for_domain
    return os.path.join(folder, token)


def _request(url, content_type=None):
    headers = {'User-Agent': user_agent}
    if content_type:
        headers['Content-Type'] = content_type
    return Request(url, headers=headers)


# #####################################################################################################
# # Algorithm support


class Algorithm(object):
    """Abstracts an algorithm (RSA or ECC)."""

    def __init__(self, name):
        """Initialize algorithm object."""
        self.name = name

    def __not_implemented(self, method):
        """Helper method to raise not implemented errors."""
        raise Exception("Algorithm {0} does not support {1}!".format(self.name, method))

    def create_key(self, key_length):
        """Create a private key of given length."""
        self.__not_implemented('create_key')


class RSA(Algorithm):
    """Abstracts the RSA algorithm."""

    def __init__(self):
        """Create new RSA algorithm object."""
        super(RSA, self).__init__("RSA")
        self.jws_algorithm = 'RS256'
        self.jws_hash = 'sha256'
        self.jws_hash_bytes = 32

    def create_key(self, key_length):
        """Generate RSA key with given key length."""
        return _run_openssl(['genrsa', str(key_length)]).decode('utf-8')


class ECC(Algorithm):
    """Abstracts Elliptic Curve based algorithms."""

    def __init__(self, curve, openssl_curve, bitlength, jws_algorithm, jws_hash, jws_hash_bytes):
        """Create new ECC algorithm object for given JOSE curve name, OpenSSL curve name, and bit length."""
        super(ECC, self).__init__("ECC-{0}".format(curve))
        self.curve = curve
        self.openssl_curve = openssl_curve
        self.bitlength = bitlength
        self.bytelength = (bitlength + 7) // 8
        self.jws_algorithm = jws_algorithm
        self.jws_hash = jws_hash
        self.jws_hash_bytes = jws_hash_bytes

    def create_key(self, key_length):
        """Generate ECC private key for this curve. The key length is ignored."""
        return _run_openssl(['ecparam', '-name', self.openssl_curve, '-genkey', '-noout']).decode('utf-8')

    def extract_point(self, pub_hex):
        """Extract the public point coordinates from the given hexadecimal description."""
        if len(pub_hex) != 2 * self.bytelength:
            raise ValueError("Key error: public key has incorrect length")
        return pub_hex[:self.bytelength], pub_hex[self.bytelength:]


_ALGORITHMS = {
    'rsa': RSA(),
    'p-256': ECC('p-256', 'prime256v1', 256, 'ES256', 'sha256', 32),
    'p-384': ECC('p-384', 'secp384r1', 384, 'ES384', 'sha384', 48),
    # 'p-521': ECC('p-521', 'secp521r1', 528, 'ES512', 'sha512', 64),
    #          -- P-521 isn't supported yet (on Let's Encrypt staging server);
    #             see https://github.com/letsencrypt/boulder/issues/2217
}


def _get_algorithm(algorithm):
    if algorithm not in _ALGORITHMS:
        raise ValueError("Unknown algorithm '{0}'!".format(algorithm))
    return _ALGORITHMS[algorithm]


# #####################################################################################################
# # Low level functions


def read_stdin():
    """Read data from stdin."""
    if sys.version_info < (3, 0):
        return sys.stdin.read()
    else:
        return sys.stdin.buffer.read()


def write_file(filename, content):
    """Write the contents (string) into the file, encoded with UTF-8."""
    with open(filename, "wb") as f:
        f.write(content.encode('utf-8'))


def create_key(key_length=4096, algorithm="rsa"):
    """Create an RSA key with the given key length in bits."""
    algorithm = _get_algorithm(algorithm)
    return algorithm.create_key(key_length)


def generate_csr(key_filename, config_filename, domains, must_staple=False):
    """Given a private key and a list of domains, create a Certificate Signing Request (CSR).

    ``must_staple```: if set to ``True``, asks for a certificate with OCSP Must Staple enabled.
    """
    # First generate config
    template = """HOME     = .
RANDFILE = $ENV::HOME/.rnd

[req]
distinguished_name = req_DN
req_extensions     = req_SAN

[req_DN]

[req_SAN]
subjectAltName = {0}
"""
    if must_staple:
        # See https://tools.ietf.org/html/rfc7633#section-6 and https://scotthelme.co.uk/ocsp-must-staple/
        template += "1.3.6.1.5.5.7.1.24 = DER:30:03:02:01:05\n"
        # For OpenSSL 1.1.0 or newer, we can use
        #     template += "tlsfeature = status_request\n"
        # instead.
    write_file(config_filename, template.format(','.join(['DNS:{0}'.format(domain) for domain in domains])))
    # Generate CSR
    if key_filename == '/dev/stdin':
        stdin = read_stdin()
        return _run_openssl(['req', '-new', '-sha256', '-key', '/dev/stdin', '-subj', '/', '-config', config_filename], input=stdin).decode('utf-8')
    else:
        return _run_openssl(['req', '-new', '-sha256', '-key', key_filename, '-subj', '/', '-config', config_filename]).decode('utf-8')


def get_csr_as_text(csr_filename):
    """Convert CSR file to plaintext with OpenSSL."""
    return _run_openssl(["req", "-in", csr_filename, "-noout", "-text"]).decode('utf-8')


def parse_account_key(account_key):
    """Parse account RSA private key to get public key.

    Returns five variables (account_key_type, account_key, account_key_algorithm, header, thumbprint)
    needed for other low-level functions.
    """
    sys.stderr.write("Parsing account key...")
    account_key_type = None
    with open(account_key, "r") as f:
        for line in f:
            m = re.match(r"^\s*-{5,}BEGIN\s+(EC|RSA)\s+PRIVATE\s+KEY-{5,}\s*$", line)
            if m is not None:
                account_key_type = m.group(1).lower()
                break
    if account_key_type not in ("rsa", "ec"):
        raise ValueError("Unknown key type '{0}'.".format(account_key_type))
    out = _run_openssl([account_key_type, "-in", account_key, "-noout", "-text"]).decode('utf8')
    if account_key_type == "rsa":
        pub_hex, pub_exp = re.search(r"modulus:\n\s+00:([a-f0-9\:\s]+?)\npublicExponent: ([0-9]+)", out, re.MULTILINE | re.DOTALL).groups()
        pub_mod = binascii.unhexlify(re.sub(r"(\s|:)", "", pub_hex))
        pub_mod64 = _b64(pub_mod)
        pub_exp = "{0:x}".format(int(pub_exp))
        pub_exp = "0{0}".format(pub_exp) if len(pub_exp) % 2 else pub_exp
        pub_exp64 = _b64(binascii.unhexlify(pub_exp))
        algorithm = _get_algorithm('rsa')
        header = {
            "alg": algorithm.jws_algorithm,
            "jwk": {
                "kty": "RSA",
                "e": pub_exp64,
                "n": pub_mod64,
            },
        }
    else:
        pub_data = re.search(
            r"pub:\s*\n\s+04:([a-f0-9\:\s]+?)\nASN1 OID: (\S+)\nNIST CURVE: (\S+)", out, re.MULTILINE | re.DOTALL)
        if pub_data is None:
            raise ValueError("Invalid or incompatible ECC key.")
        pub_hex = binascii.unhexlify(re.sub(r"(\s|:)", "", pub_data.group(1)))
        curve = pub_data.group(3).lower()
        algorithm = _get_algorithm(curve)
        x, y = algorithm.extract_point(pub_hex)
        header = {
            "alg": algorithm.jws_algorithm,
            "jwk": {
                "kty": "EC",
                "crv": curve.upper(),
                "x": _b64(x),
                "y": _b64(y),
            },
        }
    accountkey_json = json.dumps(header['jwk'], sort_keys=True, separators=(',', ':'))
    thumbprint = _b64(hashlib.sha256(accountkey_json.encode('utf8')).digest())
    sys.stderr.write(" ok ")
    sys.stderr.flush()
    return account_key_type, account_key, algorithm, header, thumbprint


def _lookup_directory(CA, *keys):
    """Helper function to look up the correct URL(s) in the server's directory.

    Returns a replay nonce (or None) and the URLs associated to the keys:
    ``(nonce, url_1, url_2, ...)``
    """
    result = urlopen(_request(CA + "/directory"))
    data = json.loads(result.read().decode('utf8'))
    urls = [data.get(key, CA + '/acme/' + key) for key in keys]
    nonce = result.headers['Replay-Nonce']
    return tuple([nonce] + urls)


def _send_signed_request(payload, header, CA, account_key_type, account_key, account_key_algorithm, key=None, url=None):
    """Helper function make signed requests. Either ``key`` or ``url`` must be specified."""
    # Make sure we know the URL, and figure out nonce_url (and see if we get a nonce as well)
    assert key is not None or url is not None
    if url is None:
        nonce, url, nonce_url = _lookup_directory(CA, key, 'new-nonce')
    else:
        nonce, nonce_url = _lookup_directory(CA, 'new-nonce')
    # If we didn't already got a nonce, ask nonce_url
    if nonce is None:
        nonce = urlopen(_request(nonce_url)).headers['Replay-Nonce']
    # Prepare JOSE object
    payload64 = _b64(json.dumps(payload).encode('utf8'))
    protected = copy.deepcopy(header)
    protected.update({"nonce": nonce})
    protected64 = _b64(json.dumps(protected).encode('utf8'))
    out = _run_openssl(["dgst", "-{0}".format(account_key_algorithm.jws_hash), "-sign", account_key], "{0}.{1}".format(protected64, payload64).encode('utf8'))
    if account_key_type == 'ec':
        out = _run_openssl(["asn1parse", "-inform", "DER"], input=out).decode("utf8")
        sig = re.findall(r"prim:\s+INTEGER\s+:([0-9A-F]{%s})\n" % (2 * account_key_algorithm.jws_hash_bytes), out)
        if len(sig) != 2:
            raise Exception("Failed to generate signature; cannot parse DER output:\n\n{0}".format(out))
        out = binascii.unhexlify(sig[0]) + binascii.unhexlify(sig[1])
    data = json.dumps({
        "header": header,
        "protected": protected64,
        "payload": payload64,
        "signature": _b64(out),
    })
    # Send JOSE object
    try:
        resp = urlopen(_request(url, 'application/jose+json'), data.encode('utf8'))
        return resp.getcode(), resp.read()
    except IOError as e:
        return getattr(e, "code", None), getattr(e, "read", e.__str__)()


def parse_csr(csr):
    """Parse a Certificate Signing Request (CSR).

    Returns the list of domains this CSR affects.
    """
    out = get_csr_as_text(csr)
    domains = set([])
    common_name = re.search(r"Subject:.*? CN\s*=\s*([^\s,;/]+)", out)
    if common_name is not None:
        domains.add(common_name.group(1))
    for subject_alt_names in re.finditer(r"X509v3 Subject Alternative Name: (?:critical)?\n +([^\n]+)\n", out, re.MULTILINE | re.DOTALL):
        for san in subject_alt_names.group(1).split(", "):
            if san.startswith("DNS:"):
                domains.add(san[4:])
    return sorted(domains)


def register_account(header, CA, account_key_type, account_key, account_key_algorithm, email_address=None, telephone=None):
    """Create account on CA server.

    Return True if the account was created and False if it already exists.
    Raises an exception in case of errors.
    """
    argreement = ca_agreement
    try:
        resp = urlopen(_request(ca_agreement_redirect_pattern.format(CA)))
        argreement = resp.url
    except IOError as e:
        sys.stderr.write("Retrieving agreement failed: {0}\n".format(e.message))
    data = {
        "resource": "new-reg",
        "agreement": argreement,
    }
    contacts = []
    if email_address is not None:
        contacts.append("mailto:{0}".format(email_address))
    if telephone is not None:
        contacts.append("tel:{0}".format(telephone))
    if len(contacts) > 0:
        data["contact"] = contacts
    code, result = _send_signed_request(data, header, CA, account_key_type, account_key, account_key_algorithm, key="new-reg")
    if code == 201:
        return True
    elif code == 409:
        return False
    else:
        raise ValueError("Error registering: {0} {1}".format(code, result))


def get_challenge(domain, header, CA, account_key_type, account_key, account_key_algorithm, thumbprint):
    """Retrieve challenge for a domain.

    Returns the challenge object, the challenge token as well as the
    content for the token file.
    """
    # get new challenge
    code, result = _send_signed_request({
        "resource": "new-authz",
        "identifier": {"type": "dns", "value": domain},
    }, header, CA, account_key_type, account_key, account_key_algorithm, key="new-authz")
    if code != 201:
        raise ValueError("Error registering: {0} {1}".format(code, result))

    # make the challenge file
    challenge = [c for c in json.loads(result.decode('utf8'))['challenges'] if c['type'] == "http-01"][0]
    challenge['token'] = re.sub(r"[^A-Za-z0-9_\-]", "_", challenge['token'])
    keyauthorization = "{0}.{1}".format(challenge['token'], thumbprint)
    return challenge, challenge['token'], keyauthorization


def get_wellknown_url(domain, token):
    """Return the URL for the token file on the server."""
    return "http://{0}/.well-known/acme-challenge/{1}".format(domain, token)


def check_challenge(domain, token, keyauthorization):
    """Check whether the token is correctly placed on the server.

    Returns True in case it is, and False in case it is not.
    """
    # check that the file is in place
    wellknown_url = get_wellknown_url(domain, token)
    try:
        resp = urlopen(_request(wellknown_url))
        return resp.read().decode('utf8').strip() == keyauthorization
    except IOError:
        return False


def notify_challenge(domain, header, CA, account_key_type, account_key, account_key_algorithm, challenge, keyauthorization):
    """Notify the CA server that the token files are available on the webserver."""
    # notify challenge are met
    code, result = _send_signed_request({
        "resource": "challenge",
        "keyAuthorization": keyauthorization,
    }, header, CA, account_key_type, account_key, account_key_algorithm, url=challenge['uri'])
    if code != 202:
        raise ValueError("Error triggering challenge: {0} {1}".format(code, result))


def check_challenge_verified(domain, challenge, wait=True):
    """Check whether the challenge has been verified by the CA server.

    Returns True in case it was successfully verified, and False in case
    the verification is not done yet. Raises an exception in case the
    verification failed.

    If wait is set to True (default), the function will not return False
    but instead loop until it either returns True or it raises an exception.
    """
    while True:
        try:
            resp = urlopen(_request(challenge['uri']))
            challenge_status = json.loads(resp.read().decode('utf8'))
        except IOError as e:
            raise ValueError("Error checking challenge: {0} {1}".format(e.code, json.loads(e.read().decode('utf8'))))
        if challenge_status['status'] == "pending":
            if wait:
                time.sleep(2)
            else:
                return False
        elif challenge_status['status'] == "valid":
            return True
        else:
            raise ValueError("{0} challenge did not pass: {1}".format(domain, challenge_status))


def retrieve_certificate(csr, header, CA, account_key_type, account_key, account_key_algorithm):
    """Retrieve the certificate from the CA server."""
    sys.stderr.write("Signing certificate...")
    csr_der = _run_openssl(["req", "-in", csr, "-outform", "DER"])
    code, result = _send_signed_request({
        "resource": "new-cert",
        "csr": _b64(csr_der),
    }, header, CA, account_key_type, account_key, account_key_algorithm, key="new-cert")
    if code != 201:
        raise ValueError("Error signing certificate: {0} {1}".format(code, result))
    return """-----BEGIN CERTIFICATE-----\n{0}\n-----END CERTIFICATE-----\n""".format("\n".join(textwrap.wrap(base64.b64encode(result).decode('utf8'), 64)))


def download_certificate(url):
    """Download a certificate (as a file) from the CA server."""
    try:
        resp = urlopen(_request(url))
        if resp.getcode() != 200:
            raise ValueError("Cannot retrieve certificate (status code {0}; message: {1})".format(resp.getcode(), resp.read()))
        return resp.read().decode('utf-8').strip()
    except IOError as e:
        raise ValueError("Cannot retrieve certificate ({0})".format(str(e)))


# #####################################################################################################
# # High level functions


def serialize_state(state):
    """Serialize the state as a string."""
    return json.dumps(state, sort_keys=True)


def deserialize_state(serialized_state):
    """Deserialize the given serialized state.

    Raises exception in case this is not a valid state.
    """
    result = json.loads(serialized_state)
    if type(result) != dict or 'account_key' not in result or 'account_key_type' not in result or 'account_key_algorithm' not in result or 'header' not in result or 'thumbprint' not in result or 'CA' not in result or 'challenges' not in result:
        raise ValueError("Not a valid serialized state!")
    return result


def get_challenges(account_key, csr, CA, email_address=None, telephone=None):
    """Set up the account and retrieve challenges from CA server.

    Returns a state object.
    """
    account_key_type, account_key, account_key_algorithm, header, thumbprint = parse_account_key(account_key)
    # find domains
    domains = parse_csr(csr)
    # get the certificate domains and expiration
    register_account(header, CA, account_key_type, account_key, account_key_algorithm, email_address=email_address, telephone=telephone)
    challenges = []
    # verify each domain
    for domain in domains:
        challenge, token, keyauthorization = get_challenge(domain, header, CA, account_key_type, account_key, account_key_algorithm, thumbprint)
        challenges.append({'domain': domain, 'challenge': challenge, 'token': token, 'keyauthorization': keyauthorization})
    return {'account_key_type': account_key_type, 'account_key_algorithm': account_key_algorithm, 'account_key': account_key, 'header': header, 'thumbprint': thumbprint, 'CA': CA, 'challenges': challenges}


def write_challenges(state, folder_for_domain):
    """Write challenge files to disk.

    If the folder_for_domain parameter is a callable, it is expected to
    return a path when called with a single parameter, which will be the
    domain name. Otherwise, it is assumed to be a string.
    """
    challenges = state['challenges']
    for challenge_entry in challenges:
        domain = challenge_entry['domain']
        token = challenge_entry['token']
        keyauthorization = challenge_entry['keyauthorization']
        wellknown_path = _get_wellknown_path(domain, token, folder_for_domain)
        write_file(wellknown_path, keyauthorization)


def remove_challenges(state, folder_for_domain):
    """Remove the challenge files from disk.

    See documentation of write_challenges() for explanation
    of folder_for_domain.
    """
    challenges = state['challenges']
    for challenge_entry in challenges:
        domain = challenge_entry['domain']
        token = challenge_entry['token']
        wellknown_path = _get_wellknown_path(domain, token, folder_for_domain)
        os.remove(wellknown_path)


def verify_challenges(state):
    """Verify that the challenge files are available on the web server with HTTP."""
    challenges = state['challenges']
    for challenge_entry in challenges:
        domain = challenge_entry['domain']
        token = challenge_entry['token']
        keyauthorization = challenge_entry['keyauthorization']
        if not check_challenge(domain, token, keyauthorization):
            raise ValueError("Couldn't download challenge file at {0}".format(get_wellknown_url(domain, token)))


def notify_challenges(state):
    """Notify the CA server that the challenges are ready."""
    challenges = state['challenges']
    for challenge_entry in challenges:
        domain = challenge_entry['domain']
        keyauthorization = challenge_entry['keyauthorization']
        challenge = challenge_entry['challenge']
        notify_challenge(domain, state['header'], state['CA'], state['account_key_type'], state['account_key'], state['account_key_algorithm'], challenge, keyauthorization)


def check_challenges(state, csr, inform=None):
    """Check the CA server for challenge results, and retrieves the certificate.

    In case inform is specified, it is called with the domain name as the only argument
    for every successfully verified domain.

    When all domains are verified, the certificate is obtained from the CA server and
    returned as a string.
    """
    challenges = state['challenges']
    for challenge_entry in challenges:
        domain = challenge_entry['domain']
        challenge = challenge_entry['challenge']
        check_challenge_verified(domain, challenge, wait=True)
        if callable(inform):
            inform(domain)
    return retrieve_certificate(csr, state['header'], state['CA'], state['account_key_type'], state['account_key'], state['account_key_algorithm'])
