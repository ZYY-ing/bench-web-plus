"""Certificates issued by this center, never by disabling TLS verification."""
from datetime import datetime, timedelta, timezone
import ipaddress
from pathlib import Path
import threading

LOCK = threading.RLock()


def issue(host, directory):
    with LOCK:
        return _issue(host, directory)


def _issue(host, directory):
    from .session_ssh import paramiko_module
    paramiko_module()  # Makes the tool's own cryptography dependency available.
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    ca_path, key_path = directory / 'ca.pem', directory / 'ca-key.pem'
    now = datetime.now(timezone.utc)
    if ca_path.exists() != key_path.exists():
        raise ValueError('证书目录不完整，请恢复原证书；不会自动更换信任身份')
    if ca_path.exists():
        ca = x509.load_pem_x509_certificate(ca_path.read_bytes())
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        if ca.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo) != key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo):
            raise ValueError('CA证书与私钥不匹配，请恢复原证书；不会自动替换信任身份')
    else:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Bench Web Plus private CA')])
        ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
              .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=5))
              .not_valid_after(now+timedelta(days=3650)).add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
              .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
              .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), critical=False)
              .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                          data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                          crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
              .sign(key, hashes.SHA256()))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        key_path.chmod(0o600)
        ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    try:
        san = x509.IPAddress(ipaddress.ip_address(host))
    except ValueError:
        san = x509.DNSName(host)
    cert = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
            .issuer_name(ca.subject).public_key(server_key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now-timedelta(minutes=5)).not_valid_after(now+timedelta(days=365))
            .add_extension(x509.SubjectAlternativeName([san, x509.DNSName('localhost'), x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), critical=False)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=True,
                                        data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                        crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .sign(key, hashes.SHA256()))
    return {'ca': ca.public_bytes(serialization.Encoding.PEM).decode(),
            'cert': cert.public_bytes(serialization.Encoding.PEM).decode(),
            'key': server_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()}
