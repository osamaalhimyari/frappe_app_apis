"""Crypto the Server Script sandbox cannot do itself.

Some web logins encrypt the password in the browser before posting it -- the
IM web app (gps.im2m.ws) uses RSA PKCS#1 v1.5 with a public key baked into its
login page (JSEncrypt). A Server Script replaying that login needs the same
encryption, and RestrictedPython has no crypto, so it calls this.
"""

import base64

import frappe
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding


@frappe.whitelist(methods=["POST"])
def rsa_encrypt(public_key, text):
	"""Base64 of RSA PKCS#1 v1.5 encryption of `text` with a PEM public key --
	exactly what JSEncrypt.encrypt() produces in a browser."""
	key = serialization.load_pem_public_key(str(public_key).strip().encode())
	return base64.b64encode(key.encrypt(str(text).encode(), padding.PKCS1v15())).decode()
