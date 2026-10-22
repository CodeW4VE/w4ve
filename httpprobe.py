"""Bounded, credential-free HTTPS checks of explicitly selected JSON endpoints."""
import http.client
import json
import math
import socket
import ssl
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit


def _check(url, *, expected_status=200, software='', capabilities=(), ca_file=None, timeout=5, dry_run=False):
    if (not isinstance(url, str) or len(url) > 2048 or any(ord(char) < 33 for char in url)
            or type(expected_status) is not int or not 100 <= expected_status <= 599
            or type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 10
            or not isinstance(software, str) or len(software) > 128
            or not isinstance(capabilities, (list, tuple)) or len(capabilities) > 32
            or any(not isinstance(key, str) or not key or len(key) > 128 for key in capabilities)):
        return {'ok': False, 'reason': 'invalid_probe'}
    try:
        target = urlsplit(url)
        port = target.port or 443
        if (target.scheme != 'https' or not target.hostname or target.username is not None
                or target.password is not None or target.query or target.fragment or not 1 <= port <= 65535):
            raise ValueError()
        path = target.path or '/'
    except ValueError:
        return {'ok': False, 'reason': 'invalid_https_endpoint'}
    if dry_run: return {'ok': True, 'checked': False, 'reason': 'dry_run'}
    connection = None
    try:
        context = ssl.create_default_context(cafile=ca_file)
        # No environment proxy, Authorization header, cookies or redirects.
        connection = http.client.HTTPSConnection(target.hostname,port,context=context,timeout=timeout)
        connection.request('GET',path,headers={'Accept': 'application/json', 'User-Agent': 'w4ve-https-probe/1'})
        response = connection.getresponse()
        body = response.read(16385)
        if len(body) > 16384: return {'ok': False, 'reason': 'response_too_large', 'status': response.status}
        if response.status != expected_status: return {'ok': False, 'reason': 'unexpected_status', 'status': response.status}
        if expected_status != 200 and not software and not capabilities:
            return {'ok': True, 'reason': 'https_status_verified', 'status': response.status}
        try:
            value = json.loads(body)
        except (ValueError, UnicodeError, RecursionError):
            return {'ok': False, 'reason': 'invalid_json', 'status': response.status}
        if not isinstance(value, dict): return {'ok': False, 'reason': 'invalid_json', 'status': response.status}
        if any(key in value and value[key] is not True for key in ('ok','ready')):
            return {'ok': False, 'reason': 'endpoint_not_ready', 'status': response.status}
        if software and value.get('software') != software:
            return {'ok': False, 'reason': 'software_mismatch', 'status': response.status}
        flags = value.get('capabilities', {})
        if capabilities and (not isinstance(flags,dict) or any(flags.get(key) is not True for key in capabilities)):
            return {'ok': False, 'reason': 'capability_unavailable', 'status': response.status}
        return {'ok': True, 'reason': 'https_json_verified', 'status': response.status}
    except ssl.SSLCertVerificationError:
        return {'ok': False, 'reason': 'certificate_verification_failed'}
    except ssl.SSLError:
        return {'ok': False, 'reason': 'tls_failed'}
    except socket.gaierror:
        return {'ok': False, 'reason': 'dns_failed'}
    except (TimeoutError, socket.timeout):
        return {'ok': False, 'reason': 'timeout'}
    except (OSError, http.client.HTTPException, ValueError):
        return {'ok': False, 'reason': 'endpoint_unavailable'}
    finally:
        if connection is not None: connection.close()


def check(url, *, expected_status=200, software='', capabilities=(), ca_file=None, timeout=5, dry_run=False):
    values = dict(url=url,expected_status=expected_status,software=software,capabilities=capabilities,
                  ca_file=str(ca_file) if ca_file is not None else None,timeout=timeout,dry_run=dry_run)
    preview = _check(**{**values,'dry_run': True})
    if not preview['ok'] or dry_run: return preview
    payload = json.dumps(values)
    if len(payload.encode()) > 16384: return {'ok': False, 'reason': 'invalid_probe'}
    try:
        # Socket timeouts alone do not bound DNS resolution or a peer that
        # trickles headers/body forever. Terminate this isolated child on a
        # wall-clock deadline as well; never inherit a shell or print stderr.
        result = subprocess.run([sys.executable,'-I',str(Path(__file__).resolve()),'--worker'],
            input=payload,capture_output=True,text=True,timeout=timeout+1)
        if result.returncode or len(result.stdout) > 1024: raise ValueError()
        answer = json.loads(result.stdout)
        if not isinstance(answer,dict) or type(answer.get('ok')) is not bool: raise ValueError()
        return answer
    except subprocess.TimeoutExpired:
        return {'ok': False, 'reason': 'deadline_exceeded'}
    except (OSError, ValueError):
        return {'ok': False, 'reason': 'probe_worker_unavailable'}


if __name__ == '__main__':
    try:
        raw = sys.stdin.buffer.read(16385)
        if len(raw) > 16384 or sys.argv[1:] != ['--worker']: raise ValueError()
        answer = _check(**json.loads(raw))
    except Exception:
        answer = {'ok': False, 'reason': 'invalid_probe'}
    print(json.dumps(answer,sort_keys=True))
