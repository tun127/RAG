"""Credential-free, bounded transport diagnostics. Never persist exception text."""
import errno
import re
import socket
import ssl

import httpx


def transport_diagnostic(exc):
    chain, seen, pending = [], set(), [exc]
    while pending and len(chain) < 8:
        item = pending.pop(0)
        if id(item) in seen:
            continue
        seen.add(id(item))
        chain.append(item)
        pending.extend(x for x in (item.__cause__, item.__context__, *item.args) if isinstance(x, BaseException))
    def has(cls):
        return any(isinstance(x, cls) for x in chain)
    # A generic ConnectError may wrap DNS, TLS or an OS access denial.
    if has(httpx.ProxyError):
        category = 'proxy_connection_failed'
    elif has(socket.gaierror):
        category = 'dns_failed'
    elif has(ssl.SSLError):
        category = 'tls_failed'
    elif has(httpx.ConnectTimeout):
        category = 'connect_timeout'
    elif has(httpx.ReadTimeout):
        category = 'read_timeout'
    elif has(httpx.ConnectError) or has(ConnectionError) or has(PermissionError):
        category = 'connection_failed'
    else:
        category = 'transport_other'
    causes = []
    for item in chain:
        reason = 'transport_exception'
        if isinstance(item, socket.gaierror):
            reason = 'name_resolution_failed'
        elif isinstance(item, ssl.SSLCertVerificationError):
            reason = 'certificate_verification_failed'
        elif isinstance(item, ssl.SSLError):
            reason = 'tls_handshake_failed'
        elif isinstance(item, PermissionError) or getattr(item, 'winerror', None) == 10013 or getattr(item, 'errno', None) in {errno.EACCES, errno.EPERM, 10013}:
            reason = 'network_access_denied_by_os_or_environment'
        elif isinstance(item, httpx.ConnectTimeout):
            reason = 'connect_deadline_exceeded'
        elif isinstance(item, httpx.ReadTimeout):
            reason = 'read_deadline_exceeded'
        elif isinstance(item, httpx.ProxyError):
            reason = 'proxy_connection_or_tunnel_failed'
        elif isinstance(item, ConnectionRefusedError):
            reason = 'connection_refused'
        elif isinstance(item, (httpx.ConnectError, ConnectionError)):
            reason = 'connection_not_established'
        causes.append({
            'exception_type': re.sub('[^A-Za-z0-9_]', '', type(item).__name__)[:80],
            'reason': reason,
            **{k: getattr(item, k) for k in ('errno', 'winerror', 'verify_code') if type(getattr(item, k, None)) is int},
        })
    return {'category': category, 'causes': causes}


def safe_transport_diagnostic(value):
    """Persistence allowlist; never pass arbitrary adapter diagnostic fields through."""
    if not isinstance(value, dict):
        return None
    categories = {'dns_failed', 'tls_failed', 'connection_failed', 'proxy_connection_failed',
                  'connect_timeout', 'read_timeout', 'transport_other'}
    reasons = {'transport_exception', 'name_resolution_failed', 'certificate_verification_failed',
               'tls_handshake_failed', 'network_access_denied_by_os_or_environment',
               'connect_deadline_exceeded', 'read_deadline_exceeded',
               'proxy_connection_or_tunnel_failed', 'connection_refused', 'connection_not_established'}
    if value.get('category') not in categories:
        return None
    causes = []
    for cause in (value.get('causes') or [])[:8]:
        if not isinstance(cause, dict):
            continue
        causes.append({
            'exception_type': re.sub('[^A-Za-z0-9_]', '', str(cause.get('exception_type', 'Exception')))[:80],
            'reason': cause.get('reason') if cause.get('reason') in reasons else 'transport_exception',
            **{k: cause[k] for k in ('errno', 'winerror', 'verify_code') if type(cause.get(k)) is int},
        })
    return {'category': value['category'], 'causes': causes}
