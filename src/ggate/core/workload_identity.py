"""Allowlisted static workload metadata. Never requests identity tokens or role credentials."""
import json
import os
import sys
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ENV_FIELDS = {'cloud_provider': ['GGATE_CLOUD_PROVIDER'],
 'cloud_account': ['GGATE_CLOUD_ACCOUNT',
                   'GOOGLE_CLOUD_PROJECT',
                   'GCP_PROJECT',
                   'AWS_ACCOUNT_ID',
                   'AZURE_SUBSCRIPTION_ID'],
 'cloud_region': ['GGATE_CLOUD_REGION',
                  'GOOGLE_CLOUD_REGION',
                  'CLOUD_RUN_REGION',
                  'AWS_REGION',
                  'AWS_DEFAULT_REGION',
                  'AZURE_REGION'],
 'cloud_zone': ['GOOGLE_CLOUD_ZONE'],
 'cloud_instance_id': ['GGATE_CLOUD_INSTANCE_ID'],
 'cloud_instance_name': ['GGATE_CLOUD_INSTANCE_NAME'],
 'cloud_instance_type': ['GGATE_CLOUD_INSTANCE_TYPE'],
 'cloud_resource_group': ['GGATE_CLOUD_RESOURCE_GROUP', 'AZURE_RESOURCE_GROUP'],
 'service_identity': ['GGATE_WORKLOAD_SERVICE_IDENTITY',
                      'GGATE_WORKLOAD_SERVICE_ACCOUNT',
                      'GOOGLE_SERVICE_ACCOUNT_EMAIL',
                      'AWS_ROLE_ARN',
                      'AZURE_CLIENT_ID'],
 'cloud_run_service': ['K_SERVICE'],
 'cloud_run_revision': ['K_REVISION'],
 'kubernetes_namespace': ['POD_NAMESPACE', 'KUBERNETES_NAMESPACE'],
 'kubernetes_pod_name': ['POD_NAME'],
 'kubernetes_pod_uid': ['POD_UID'],
 'kubernetes_node_name': ['NODE_NAME'],
 'kubernetes_service_account': ['POD_SERVICE_ACCOUNT', 'KUBERNETES_SERVICE_ACCOUNT'],
 'function_name': ['AWS_LAMBDA_FUNCTION_NAME', 'FUNCTION_NAME', 'WEBSITE_SITE_NAME'],
 'function_version': ['AWS_LAMBDA_FUNCTION_VERSION', 'K_REVISION'],
 'workload_instance_id': ['AWS_LAMBDA_LOG_STREAM_NAME', 'WEBSITE_INSTANCE_ID'],
 'cloud_task_arn': ['ECS_TASK_ARN']}

def sanitize_identity(values):
    return {key: value.strip() for key, value in values.items() if key in ENV_FIELDS and isinstance(value, str)
            and value.strip() and len(value.encode()) <= 253 and not any(ord(c) < 32 or ord(c) == 127 for c in value)}

def environment_identity(env=None):
    env = os.environ if env is None else env
    found = {key: next((env[name] for name in names if env.get(name, '').strip()), '') for key, names in ENV_FIELDS.items()}
    if not found['cloud_provider']:
        if env.get('AWS_LAMBDA_FUNCTION_NAME') or env.get('AWS_ROLE_ARN') or env.get('ECS_TASK_ARN'): found['cloud_provider'] = 'aws'
        elif env.get('K_SERVICE') or env.get('GOOGLE_CLOUD_PROJECT'): found['cloud_provider'] = 'gcp'
        elif env.get('WEBSITE_SITE_NAME') or env.get('AZURE_CLIENT_ID'): found['cloud_provider'] = 'azure'
    if env.get('K_SERVICE') and env.get('HOSTNAME'): found['workload_instance_id'] = found['workload_instance_id'] or env['HOSTNAME']
    return sanitize_identity(found)

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None

def _metadata(path, headers=None, method='GET', google=False):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    request = urllib.request.Request('http://169.254.169.254' + path, headers=headers or {}, method=method)
    with opener.open(request, timeout=1) as response:
        if google and response.headers.get('Metadata-Flavor') != 'Google': raise ValueError('Not GCP metadata')
        body = response.read(16385)
        if len(body) > 16384: raise ValueError('Metadata too large')
        return body.decode().strip()

def _aws():
    token = _metadata('/latest/api/token', {'X-aws-ec2-metadata-token-ttl-seconds': '60'}, 'PUT')
    headers = {'X-aws-ec2-metadata-token': token}
    doc = json.loads(_metadata('/latest/dynamic/instance-identity/document', headers))
    result = {'cloud_provider': 'aws', **{target: doc.get(source) for source, target in {
        'accountId':'cloud_account','instanceId':'cloud_instance_id','region':'cloud_region','availabilityZone':'cloud_zone','instanceType':'cloud_instance_type'}.items()}}
    try: result['service_identity'] = _metadata('/latest/meta-data/iam/security-credentials/', headers)
    except Exception: pass
    return sanitize_identity(result)

def _gcp():
    def get(path): return _metadata('/computeMetadata/v1/' + path, {'Metadata-Flavor':'Google'}, google=True)
    result = {'cloud_provider':'gcp','cloud_account':get('project/project-id')}
    fields = {'instance/id':'cloud_instance_id','instance/name':'cloud_instance_name','instance/zone':'cloud_zone',
              'instance/machine-type':'cloud_instance_type','instance/service-accounts/default/email':'service_identity'}
    def read(item):
        path, key = item
        try: return key, get(path)
        except Exception: return key, ''
    with ThreadPoolExecutor(max_workers=5) as pool:
        result.update(pool.map(read, fields.items()))
    for key in ('cloud_zone','cloud_instance_type'): result[key] = result.get(key, '').split('/')[-1]
    if result.get('cloud_zone'): result['cloud_region'] = result['cloud_zone'].rsplit('-', 1)[0]
    return sanitize_identity(result)

def _azure():
    doc = json.loads(_metadata('/metadata/instance/compute?api-version=2021-02-01&format=json', {'Metadata':'true'}))
    return sanitize_identity({'cloud_provider':'azure', **{target: doc.get(source) for source, target in {
        'subscriptionId':'cloud_account','vmId':'cloud_instance_id','name':'cloud_instance_name','location':'cloud_region',
        'zone':'cloud_zone','vmSize':'cloud_instance_type','resourceGroupName':'cloud_resource_group'}.items()}})

class WorkloadIdentity:
    def __init__(self, enabled=False, explicit=None):
        self.enabled, self.explicit = enabled, explicit or {}
        self._value = None
        self._lock = threading.Lock()

    def get(self):
        if not self.enabled: return {}
        with self._lock:
            if self._value is None:
                cloud = {}
                if sys.platform.startswith(('linux', 'win')):
                    def probe(fn):
                        try: return fn()
                        except Exception: return {}
                    with ThreadPoolExecutor(max_workers=3) as pool:
                        results = list(pool.map(probe, (_gcp, _aws, _azure)))
                    cloud = next((r for r in results if r), {})
                self._value = {**cloud, **environment_identity(), **sanitize_identity(self.explicit)}
            return dict(self._value)
