#!/usr/bin/env python3
"""The job-runner's fence, evaluated offline: can the portal-held token start a Job as the operator?

🚨 WHY THIS EXISTS. `deploy/aks/manifests/hosting-operator/jobrunner.yaml` gives the token the portal
holds `create jobs` in memex-ops. A Role cannot see inside the object it lets you create, so for as
long as nothing else held it, that token could write `serviceAccountName: hosting-operator` and run
any command with the operator's cluster-wide rights and its Azure identity. The fence is now a
ValidatingAdmissionPolicy in the same file, and THIS is what proves it holds without a cluster: the
policy's own CEL is evaluated against admission requests shaped like the API server's.

The NEGATIVE CONTROL is `test_operator_identity_job_is_denied`: the exact Job the Hosting plugin's
`HostingOperator.JobManifest` writes (serviceAccountName hosting-operator). Run this file against a
jobrunner.yaml without the policy and every denial test fails, because the policy is not found.
`test_default_identity_job_is_admitted` is the positive control: a policy that denied everything
would pass every denial below and still break the one shape the token is for.

What this CANNOT tell you: whether the API server's CEL agrees with cel-python on every expression,
or whether the cluster enforces ValidatingAdmissionPolicy at all (Kubernetes 1.30+). The expressions
are kept to the subset both implement (has(), ternaries, all(), string startsWith/endsWith, list +).

Needs `cel-python` (pip install cel-python==0.5.0) and PyYAML.
"""
import os
from pathlib import Path
import unittest

import celpy
import yaml

MANIFEST = Path(os.environ.get(
    'JOBRUNNER_UNDER_TEST',
    Path(__file__).resolve().parents[1] / 'manifests' / 'hosting-operator' / 'jobrunner.yaml'))
NAMESPACE = MANIFEST.with_name('namespace.yaml')

JOBRUNNER = 'system:serviceaccount:memex:hosting-jobrunner'
POLICY_NAME = 'hosting-jobrunner-no-privileged-identity'


def docs(path: Path) -> list:
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


def find(kind: str, name: str | None = None) -> dict | None:
    for d in docs(MANIFEST):
        if d.get('kind') == kind and (name is None or d['metadata']['name'] == name):
            return d
    return None


class Policy:
    """The ValidatingAdmissionPolicy, compiled once: match conditions, variables, validations."""

    def __init__(self, spec: dict, binding: dict | None):
        self.spec = spec
        self.binding = binding
        self.env = celpy.Environment()
        self.match = [self._compile(m['expression']) for m in spec.get('matchConditions', [])]
        self.variables = [(v['name'], self._compile(v['expression'])) for v in spec.get('variables', [])]
        self.validations = [(self._compile(v['expression']), v.get('message', '')) for v in spec['validations']]

    def _compile(self, expression: str):
        return self.env.program(self.env.compile(expression))

    def _rule_matches(self, resource: str, group: str, operation: str) -> bool:
        for rule in self.spec['matchConstraints']['resourceRules']:
            if group in rule['apiGroups'] and resource in rule['resources'] and operation in rule['operations']:
                return True
        return False

    def admit(self, username: str, obj: dict, operation: str = 'CREATE') -> tuple[bool, str]:
        """(admitted, why) — the way the API server would decide this request under the binding."""
        kind = obj['kind']
        resource, group = {'Job': ('jobs', 'batch'), 'CronJob': ('cronjobs', 'batch'), 'Pod': ('pods', '')}[kind]
        if not self._rule_matches(resource, group, operation):
            return True, 'not matched by matchConstraints'
        activation = {
            'request': celpy.json_to_cel({'userInfo': {'username': username}, 'operation': operation}),
            'object': celpy.json_to_cel(obj),
        }
        for program in self.match:
            if not bool(program.evaluate(activation)):
                return True, 'not matched by matchConditions'
        # Each variable sees the ones declared before it, as the API server composes them.
        variables = celpy.celtypes.MapType({})
        for name, program in self.variables:
            activation['variables'] = variables
            value = program.evaluate(activation)
            if isinstance(value, celpy.CELEvalError):
                return False, f'variable {name} could not be evaluated: {value}'
            variables = celpy.celtypes.MapType({**variables, celpy.celtypes.StringType(name): value})
        activation['variables'] = variables
        for program, message in self.validations:
            result = program.evaluate(activation)
            if isinstance(result, celpy.CELEvalError):
                # failurePolicy Fail: an expression the server cannot evaluate refuses the request.
                return False, f'evaluation error: {result}'
            if not bool(result):
                return False, message
        return True, 'every validation held'


def operator_job(service_account: str | None = 'hosting-operator', **pod_extra) -> dict:
    """The Job HostingOperator.JobManifest writes (MeshWeaver.Plugins), with the identity swappable."""
    pod = {
        'restartPolicy': 'Never',
        'containers': [{
            'name': 'operator',
            'image': 'registry.example.com/hosting-operator:main',
            'command': ['/bin/bash', '/opt/hosting/bin/run.sh'],
            'env': [{'name': 'HOSTING_PLAN', 'value': 'ZXhhbXBsZQ=='}],
        }],
    }
    if service_account is not None:
        pod['serviceAccountName'] = service_account
    pod.update(pod_extra)
    return {
        'apiVersion': 'batch/v1', 'kind': 'Job',
        'metadata': {'name': 'hosting-reconcile-x', 'namespace': 'memex-ops'},
        'spec': {'backoffLimit': 0, 'template': {'metadata': {}, 'spec': pod}},
    }


class JobRunnerFence(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        spec = find('ValidatingAdmissionPolicy', POLICY_NAME)
        binding = find('ValidatingAdmissionPolicyBinding', POLICY_NAME)
        cls.policy = Policy(spec['spec'], binding) if spec else None

    def require_policy(self) -> Policy:
        self.assertIsNotNone(self.policy, f'{MANIFEST} carries no ValidatingAdmissionPolicy {POLICY_NAME!r}: '
                                          'nothing stops the job-runner from starting a Job as hosting-operator')
        return self.policy

    def assertDenied(self, obj: dict, username: str = JOBRUNNER, operation: str = 'CREATE'):
        admitted, why = self.require_policy().admit(username, obj, operation)
        self.assertFalse(admitted, f'ADMITTED ({why}) — {obj}')
        # A denial must come from a validation that EVALUATED to false. An expression that errors
        # also refuses under failurePolicy Fail — but it refuses everything, so it is a broken
        # policy, not a fence, and must not count here.
        self.assertNotIn('evaluation error', why)
        self.assertNotIn('could not be evaluated', why)

    def assertAdmitted(self, obj: dict, username: str = JOBRUNNER, operation: str = 'CREATE'):
        admitted, why = self.require_policy().admit(username, obj, operation)
        self.assertTrue(admitted, f'denied: {why}')

    # ── the wiring that makes the policy real ───────────────────────────────────────────────────────

    def test_policy_fails_closed_and_is_bound_to_deny(self):
        policy = self.require_policy()
        self.assertEqual(policy.spec.get('failurePolicy'), 'Fail')
        self.assertIsNotNone(policy.binding, 'a policy with no binding is never evaluated')
        self.assertEqual(policy.binding['spec']['policyName'], POLICY_NAME)
        self.assertIn('Deny', policy.binding['spec']['validationActions'])
        self.assertNotIn('matchResources', policy.binding['spec'],
                         'the binding must not narrow which namespaces the fence covers')

    def test_matcher_names_the_bound_subject(self):
        """The policy matches the job-runner by NAME; renaming the SA in the binding alone would open it."""
        binding = find('RoleBinding', 'hosting-jobrunner')
        self.assertIsNotNone(binding)
        for subject in binding['subjects']:
            username = f"system:serviceaccount:{subject['namespace']}:{subject['name']}"
            self.assertDenied(operator_job(), username=username)

    def test_role_grants_no_secret_and_no_pod_write(self):
        role = find('Role', 'hosting-jobrunner')
        for rule in role['rules']:
            self.assertNotIn('secrets', rule['resources'], 'a namespace-wide secrets grant reads the operator\'s synced vault Secret')
            self.assertNotIn('*', rule['resources'])
            self.assertNotIn('*', rule['verbs'])
            if 'pods' in rule['resources']:
                self.assertFalse({'create', 'update', 'patch'} & set(rule['verbs']))

    def test_ops_namespace_enforces_pod_security_baseline(self):
        labels = docs(NAMESPACE)[0]['metadata'].get('labels', {})
        self.assertIn(labels.get('pod-security.kubernetes.io/enforce'), ('baseline', 'restricted'),
                      'without it a Job running as default can still mount the node and read the operator pod\'s token')

    # ── the negative control and the denials ────────────────────────────────────────────────────────

    def test_operator_identity_job_is_denied(self):
        self.assertDenied(operator_job('hosting-operator'))

    def test_any_other_named_identity_is_denied(self):
        self.assertDenied(operator_job('some-future-identity'))

    def test_deprecated_service_account_alias_is_denied(self):
        self.assertDenied(operator_job(None, serviceAccount='hosting-operator'))

    def test_default_name_plus_operator_alias_is_denied(self):
        self.assertDenied(operator_job('default', serviceAccount='hosting-operator'))

    def test_update_to_operator_identity_is_denied(self):
        self.assertDenied(operator_job('hosting-operator'), operation='UPDATE')

    def test_cronjob_with_operator_identity_is_denied(self):
        job = operator_job('hosting-operator')
        cron = {'apiVersion': 'batch/v1', 'kind': 'CronJob', 'metadata': {'name': 'c'},
                'spec': {'schedule': '* * * * *', 'jobTemplate': {'spec': job['spec']}}}
        self.assertDenied(cron)

    def test_pod_with_operator_identity_is_denied(self):
        pod = {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': 'p'},
               'spec': operator_job('hosting-operator')['spec']['template']['spec']}
        self.assertDenied(pod)

    def test_secret_volume_is_denied(self):
        self.assertDenied(operator_job(None, volumes=[{'name': 's', 'secret': {'secretName': 'hosting-operator-keyvault'}}]))

    def test_csi_volume_is_denied(self):
        self.assertDenied(operator_job(None, volumes=[{'name': 'kv', 'csi': {
            'driver': 'secrets-store.csi.k8s.io', 'volumeAttributes': {'secretProviderClass': 'hosting-operator-keyvault'}}}]))

    def test_projected_secret_is_denied(self):
        self.assertDenied(operator_job(None, volumes=[{'name': 'p', 'projected': {'sources': [
            {'configMap': {'name': 'c'}}, {'secret': {'name': 'hosting-operator-token'}}]}}]))

    def test_secret_env_from_is_denied(self):
        job = operator_job(None)
        job['spec']['template']['spec']['containers'][0]['envFrom'] = [{'secretRef': {'name': 'hosting-operator-keyvault'}}]
        self.assertDenied(job)

    def test_secret_key_ref_in_init_container_is_denied(self):
        self.assertDenied(operator_job(None, initContainers=[{'name': 'i', 'image': 'busybox', 'env': [
            {'name': 'X', 'valueFrom': {'secretKeyRef': {'name': 'hosting-operator-keyvault', 'key': 'k'}}}]}]))

    # ── the positive controls ───────────────────────────────────────────────────────────────────────

    def test_default_identity_job_is_admitted(self):
        self.assertAdmitted(operator_job(None))
        self.assertAdmitted(operator_job('default'))

    def test_config_map_and_downward_api_are_admitted(self):
        self.assertAdmitted(operator_job(None, volumes=[
            {'name': 'c', 'configMap': {'name': 'c'}},
            {'name': 'p', 'projected': {'sources': [{'configMap': {'name': 'c'}}, {'downwardAPI': {'items': []}}]}},
            {'name': 't', 'emptyDir': {}}]))

    def test_other_requesters_are_not_matched(self):
        """The fence is about the PORTAL's token. The operator's own lanes and the secret writer's
        job-runner (its own fence, secret-writer.yaml) are not matched — and neither is the Job
        controller creating the pods of a Job that was already checked."""
        for username in ('system:serviceaccount:control:hosting-secret-jobrunner',
                         'system:serviceaccount:kube-system:job-controller',
                         'github-actions-deploy',
                         'system:serviceaccount:memex:hosting-jobrunner-extra'):
            self.assertAdmitted(operator_job('hosting-operator'), username=username)

    def test_jobrunner_in_any_namespace_is_matched(self):
        self.assertDenied(operator_job('hosting-operator'), username='system:serviceaccount:another-portal:hosting-jobrunner')


if __name__ == '__main__':
    unittest.main(verbosity=2)
