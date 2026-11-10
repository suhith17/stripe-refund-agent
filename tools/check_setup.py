"""Check a clean source copy/database using the existing interpreter, offline."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from uuid import uuid4


WORKER = r'''
import json
from pathlib import Path
import payresolve
from payresolve import create_app
from payresolve.config import Settings

root = Path.cwd().resolve()
assert Path(payresolve.__file__).resolve().is_relative_to(root)
assert not (root / '.env').exists()
db = root / 'instance' / 'fresh.db'
assert not db.exists()
settings = Settings(db)
assert not settings.gemini_api_key and not settings.stripe_secret_key
client = create_app(settings).test_client()
paths = ('/health', '/', '/demo', '/static/case.css', '/static/dashboard.js')
checks = {path: client.get(path).status_code for path in paths}
assert all(code == 200 for code in checks.values()), checks
created = client.post('/requests', json={'message': 'Synthetic setup check'})
assert created.status_code == 201
location = created.headers['Location']
reopened = create_app(settings).test_client().get(location)
assert reopened.status_code == 200
assert reopened.get_json()['message'] == 'Synthetic setup check'
print(json.dumps({'routes': checks, 'fresh_database_initialized': True,
                  'request_survived_app_recreation': True,
                  'imports_from_clean_copy': True, 'credentials_loaded': False}))
'''


def main():
    root = Path(__file__).resolve().parents[1]
    run_id = uuid4().hex
    destination = root / 'instance' / 'setup-checks' / run_id
    source_copy = destination / 'source'
    source_copy.mkdir(parents=True)
    digest = hashlib.sha256()
    paths = sorted(p for p in (root / 'payresolve').rglob('*')
                   if p.is_file() and p.suffix in ('.py', '.html', '.css', '.js', '.svg'))
    paths.append(root / 'requirements.txt')
    for path in paths:
        relative = path.relative_to(root)
        target = source_copy / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        digest.update(relative.as_posix().encode() + b'\0' + path.read_bytes() + b'\0')
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('GEMINI_', 'STRIPE_')) and k not in ('DATABASE_PATH', 'PYTHONPATH')}
    dependencies = subprocess.run([sys.executable, '-m', 'pip', 'check'],
                                  capture_output=True, text=True, env=env, timeout=60)
    smoke = subprocess.run([sys.executable, '-c', WORKER], cwd=source_copy,
                           capture_output=True, text=True, env=env, timeout=60)
    report = {
        'run_id': run_id, 'created_at': datetime.now(timezone.utc).isoformat(),
        'evidence_type': 'existing_environment_clean_source_and_database',
        'python': sys.version.split()[0], 'source_sha256': digest.hexdigest(),
        'copied_files': len(paths), 'dependency_check_passed': dependencies.returncode == 0,
        'smoke_passed': smoke.returncode == 0, 'provider_calls': 0,
        'fresh_environment_install': False,
        'checks': json.loads(smoke.stdout) if smoke.returncode == 0 else None,
    }
    report_path = destination / 'report.json'
    report_path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({**report, 'report_path': str(report_path)}, indent=2))
    if dependencies.returncode:
        print(dependencies.stdout, file=sys.stderr)
    if smoke.returncode:
        print(smoke.stderr, file=sys.stderr)
    return int(dependencies.returncode != 0 or smoke.returncode != 0)


if __name__ == '__main__':
    sys.exit(main())
