#!/usr/bin/env python3
"""Exercise the installer's live-storage verdict without a cluster or credentials."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import importlib.util
import sys

_spec = importlib.util.spec_from_file_location('recovery', Path(__file__).with_name('test-observability-recovery.py'))
_recovery = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_recovery)
invoke_toolset = _recovery.invoke_toolset

SCRIPT = Path(os.environ.get('INSTALLER_UNDER_TEST', Path(__file__).with_name('install-observability.sh'))).resolve()


class StorageVerdictTest(unittest.TestCase):
    def run_installer(self, scenario, values='loki: {}\n', inspect=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'helm').write_text('#!/bin/sh\necho "$1" >> "$HELM_CALLS"\nexit 0\n')
            (root / 'kubectl').write_text('''#!/bin/sh
# The invoke pod has no kubeconfig: a connection flag drops kubectl to localhost:8080.
case " $* " in
  *' --request-timeout'*|*' --server'*|*' --kubeconfig'*|*' --token'*|*' --context'*)
    echo 'The connection to the server localhost:8080 was refused' >&2; exit 1 ;;
esac
case "$*" in
  *'get pvc'*'--ignore-not-found'*)
    echo "$*" >> "$PVC_CALLS"
    [ "$SCENARIO" = claim_read_error ] && exit 9
    case "$SCENARIO" in claim_absent|create_fails) exit 0 ;; esac
    printf 'persistentvolumeclaim/storage-loki-0\\n' ;;
  *'create -f -'*)
    cat > "$CREATED"
    [ "$SCENARIO" = create_fails ] && exit 9
    exit 0 ;;
  *volumeMounts*)
    [ "$SCENARIO" = read_error ] && exit 9
    [ "$SCENARIO" = missing_mount ] && exit 0
    printf storage ;;
  *persistentVolumeClaim.claimName*)
    [ "$SCENARIO" = claim_error ] && exit 9
    [ "$SCENARIO" = empty_dir ] && exit 0
    printf storage-loki-0 ;;
  *status.phase*)
    [ "$SCENARIO" = pvc_error ] && exit 9
    if [ "$SCENARIO" = pending ]; then printf Pending; else printf Bound; fi ;;
  *'get pods'*) printf 'loki-0 Running\\n' ;;
  *'get deploy,sts,ds'*) printf 'statefulset.apps/loki\\n' ;;
  *'rollout status'*) exit 0 ;;
  *'get deploy loki-prometheus-server'*) exit 1 ;;
  *'get pod loki-prometheus-tsdb-recovery'*) exit 0 ;;
  *configmap*|*namespace*) exit 0 ;;
  *) exit 8 ;;
esac
''')
            for name in ('helm', 'kubectl'):
                (root / name).chmod(0o755)
            (root / 'values.yaml').write_text(values)
            # The invoke pod's distroless toolset (no sed) — shared with the recovery test.
            (root / 'invoke-bin').mkdir()
            invoke_toolset(root / 'invoke-bin')
            env = dict(os.environ, PATH=str(root) + os.pathsep + str(root / 'invoke-bin'),
                       GRAFANA_PW='synthetic-test-only', VALUES=str(root / 'values.yaml'),
                       SCENARIO=scenario, CREATED=str(root / 'created.yaml'),
                       PVC_CALLS=str(root / 'pvc-calls'), HELM_CALLS=str(root / 'helm-calls'))
            result = subprocess.run([shutil.which('bash'), str(SCRIPT)], env=env, capture_output=True,
                                    text=True, timeout=10)
            if inspect is not None:
                inspect(root, result)
            return result

    def test_bound_claim_passes(self):
        result = self.run_installer('bound')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Verified:', result.stdout)

    def test_ephemeral_or_unverified_storage_fails(self):
        for scenario in ('read_error', 'missing_mount', 'claim_error', 'empty_dir', 'pvc_error', 'pending'):
            with self.subTest(scenario=scenario):
                result = self.run_installer(scenario)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn('FATAL:', result.stderr)
                self.assertNotIn('Verified:', result.stdout)



# The shipped declaration's shape: a named claim under loki.persistence, among sibling sections
# whose own `persistence:`/`size:` keys must NOT be read as Loki's.
NAMED_CLAIM_VALUES = """\
loki:
  resources:
    requests:
      memory: 512Mi
  persistence:
    enabled: true
    existingClaim: storage-loki-0   # the live store
    size: 30Gi
    storageClassName: default
  config:
    compactor:
      retention_enabled: true
prometheus:
  server:
    persistentVolume:
      size: 8Gi
"""


def read(path):
    return path.read_text() if path.exists() else ''


class DeclaredClaimTest(unittest.TestCase):
    """Step 1b: with `loki.persistence.existingClaim` the chart creates no claim, so the installer does."""

    run_installer = StorageVerdictTest.run_installer

    def test_absent_claim_is_created_from_the_declaration_before_the_upgrade(self):
        seen = {}
        result = self.run_installer('claim_absent', NAMED_CLAIM_VALUES,
                                    lambda root, _: seen.update(created=read(root / 'created.yaml'),
                                                                helm=read(root / 'helm-calls')))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Created Loki\'s declared claim monitoring/storage-loki-0 (30Gi, default)', result.stdout)
        created = seen['created']
        for line in ('kind: PersistentVolumeClaim', 'name: storage-loki-0', 'namespace: monitoring',
                     'storageClassName: default', 'storage: 30Gi', 'accessModes: [ReadWriteOnce]'):
            self.assertIn(line, created)
        self.assertNotIn('8Gi', created, 'a sibling section\'s size was read as Loki\'s')
        self.assertIn('upgrade', seen['helm'])

    def test_the_shipped_values_name_the_live_claim(self):
        # The real file, through the installer's own reader: the claim it would create is the live store.
        shipped = Path(__file__).with_name('values.observability.yaml').read_text()
        result = self.run_installer('claim_absent', shipped)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Created Loki's declared claim monitoring/storage-loki-0 (30Gi, default)", result.stdout)

    def test_existing_claim_is_never_touched(self):
        seen = {}
        result = self.run_installer('bound', NAMED_CLAIM_VALUES,
                                    lambda root, _: seen.update(created=(root / 'created.yaml').exists()))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('exists — mounted as is', result.stdout)
        self.assertFalse(seen['created'], 'an existing claim was re-created')

    def test_no_named_claim_means_no_claim_step(self):
        # Negative control: a template-based declaration (no existingClaim) never asks for the claim.
        seen = {}
        result = self.run_installer('bound', 'loki:\n  persistence:\n    enabled: true\n    size: 30Gi\n',
                                    lambda root, _: seen.update(asked=read(root / 'pvc-calls')))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(seen['asked'], '')

    def test_unreadable_or_uncreatable_claim_stops_before_the_upgrade(self):
        for scenario in ('claim_read_error', 'create_fails'):
            with self.subTest(scenario=scenario):
                seen = {}
                result = self.run_installer(scenario, NAMED_CLAIM_VALUES,
                                            lambda root, _: seen.update(helm=read(root / 'helm-calls')))
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn('FATAL:', result.stderr)
                self.assertNotIn('upgrade', seen['helm'])

    def test_named_claim_without_persistence_enabled_is_refused(self):
        # The chart mounts existingClaim only when persistence is enabled: creating it otherwise is a stray PVC.
        for enabled in ('    enabled: false\n', ''):
            with self.subTest(enabled=enabled.strip() or 'absent'):
                values = ('loki:\n  persistence:\n' + enabled
                          + '    existingClaim: storage-loki-0\n    size: 30Gi\n    storageClassName: default\n')
                seen = {}
                result = self.run_installer('claim_absent', values,
                                            lambda root, _: seen.update(created=(root / 'created.yaml').exists(),
                                                                        helm=read(root / 'helm-calls')))
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn('persistence.enabled is', result.stderr)
                self.assertFalse(seen['created'])
                self.assertNotIn('upgrade', seen['helm'])

    def test_multi_token_value_is_refused_not_truncated(self):
        # "my claim" must never become a claim named "my" while the chart renders "my claim".
        values = ('loki:\n  persistence:\n    enabled: true\n    existingClaim: "my claim"\n'
                  '    size: 30Gi\n    storageClassName: default\n')
        seen = {}
        result = self.run_installer('claim_absent', values,
                                    lambda root, _: seen.update(created=(root / 'created.yaml').exists(),
                                                                asked=read(root / 'pvc-calls'),
                                                                helm=read(root / 'helm-calls')))
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('single-token scalars only', result.stderr)
        self.assertFalse(seen['created'])
        self.assertEqual(seen['asked'], '')
        self.assertNotIn('upgrade', seen['helm'])

    def test_named_claim_without_size_is_refused_not_guessed(self):
        values = 'loki:\n  persistence:\n    enabled: true\n    existingClaim: storage-loki-0\n'
        seen = {}
        result = self.run_installer('claim_absent', values,
                                    lambda root, _: seen.update(created=(root / 'created.yaml').exists(),
                                                                helm=read(root / 'helm-calls')))
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('not its size and storageClassName', result.stderr)
        self.assertFalse(seen['created'])
        self.assertNotIn('upgrade', seen['helm'])


if __name__ == '__main__':
    unittest.main()
