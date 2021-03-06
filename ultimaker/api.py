from typing import NamedTuple
from collections import OrderedDict
from typing import Dict
import json
import datetime
import base64
import io
import shelve

from zeroconf import ServiceInfo
import requests
from requests.auth import HTTPDigestAuth
from uuid import UUID
from PIL import Image
import imagehash


# The mDNS response looks like this:
#   ServiceInfo(
#       type='_printer._tcp.local.',
#       name='ultimakersystem-REDACTED._printer._tcp.local.',
#       address=b'\xc0\xa8\x01\x12',
#       port=80,
#       weight=0,
#       priority=0,
#       server='ultimakersystem-REDACTED.local.',
#       properties={
#           b'type': b'printer',
#           b'hotend_serial_0': b'REDACTED',
#           b'cluster_size': b'1',
#           b'firmware_version': b'4.3.3.20180529',
#           b'machine': b'9066.0',
#           b'name': b'U1',
#           b'hotend_type_0': b'AA 0.4'
#       }
#   )


class Credentials(NamedTuple):
    '''A username/password pair used for HTTP Digest Authentication'''
    id: str
    key: str

class Identity(NamedTuple):
    '''An application/user name pair displayed on the printer when requesting authorization'''
    application: str
    user: str


ULTIMAKER_DATETIME_FORMAT = '%Y-%m-%dT%H:%M:%S.%fZ'

# {
#   "time_elapsed": 0,
#   "time_total": 0,
#   "datetime_started": "2018-10-10T00:46:40.776Z",
#   "datetime_finished": "2018-10-10T00:46:40.776Z",
#   "datetime_cleaned": "2018-10-10T00:46:40.776Z",
#   "source": "string",
#   "source_user": "string",
#   "source_application": "string",
#   "name": "string",
#   "uuid": "string",
#   "reprint_original_uuid": "string",
#   "state": "none"
# }
class PrintJob(NamedTuple):
    time_elapsed: datetime.timedelta
    time_total: datetime.timedelta
    datetime_started: datetime.datetime
    datetime_finished: datetime.datetime
    datetime_cleaned: datetime.datetime
    source: str
    source_user: str
    source_application: str
    name: str
    uuid: UUID
    reprint_original_uuid: UUID
    state: str
    progress: float
    pause_source: str
    result: str

    @classmethod
    def parse(cls: 'PrintJob', dct: Dict) -> 'PrintJob':
        print_job_dict = {}
        for field, value in dct.items():
            if field.startswith('time'):
                print_job_dict[field] = datetime.timedelta(seconds=value)
            elif field.startswith('datetime'):
                print_job_dict[field] = datetime.datetime.strptime(dct[field], ULTIMAKER_DATETIME_FORMAT)
            else: # Typecast
                print_job_dict[field] = cls.__annotations__[field](value)
        return PrintJob(**print_job_dict)
    
    def as_str_dict(self) -> Dict[str, str]:
        return {field: str(value) for field, value in self._asdict().items()}


class Printer():
    def __init__(self, address: str, port: int, identity: Identity, credentials: Credentials = None, timeout: float = 0.75):
        self.address = address
        self.host = f'{address}:{port}'
        self.identity = identity
        self.credentials = credentials
        self.timeout = timeout
        self.name = None
        self.guid = None
        self.camera_snapshot_uri = None

    def acquire_credentials(self):
        credentials_json = self.post_auth_request()
        self.set_credentials(Credentials(**credentials_json))

    def get_credentials(self) -> Credentials:
        if self.credentials is None:
            self.acquire_credentials()
        elif not self.get_auth_verify():
            self.credentials = None
            self.acquire_credentials()
        return self.credentials

    def set_credentials(self, credentials: Credentials):
        self.credentials = credentials

    def digest_auth(self) -> HTTPDigestAuth:
        credentials = self.get_credentials()
        return HTTPDigestAuth(credentials.id, credentials.key)

    def is_authorized(self) -> bool:
        self.get_credentials()
        return self.get_auth_check() == 'authorized'

    def into_ultimaker_json(self) -> Dict[str, str]:
        try:
            status = self.get_printer_status()
            ultimaker_json = {
                'system': {
                    'name': self.get_system_name(),
                },
                'printer': {
                    'status': status,
                },
                'camera': {
                    'snapshot': self.get_camera_snapshot_uri()
                }
            }
            if status == 'printing':
                print_job: PrintJob = self.get_print_job()
                ultimaker_json['print_job'] = print_job.as_str_dict()
            return ultimaker_json
        except requests.exceptions.Timeout:
            print(f'Timeout while generating ultimaker json')
            return {
                'system': {
                    'name': self.name,
                },
            }
        except requests.exceptions.RequestException as e:
            print(f'Exception while generating ultimaker json {e}')
            raise

    # All of the request functions below are from the Ultimaker Swagger Api available at http://PRINTER_ADDRESS/docs/api/
    # You can usually only call things other than /auth/check and /auth/request when you have credentials. As far as I've
    # tested, you don't need credentials for get queries. To be on the safe side, credentials are requested.
    # -------------------------------------------------------------------------------------------------------------------

    def post_auth_request(self) -> Dict:
        return requests.post(url=f"http://{self.host}/api/v1/auth/request", data={'application': self.identity.application, 'user': self.identity.user}, timeout=self.timeout).json()

    # Returns the response from an authorization check
    def get_auth_check(self) -> str:
        return requests.get(url=f"http://{self.host}/api/v1/auth/check/{self.credentials.id}", timeout=self.timeout).json()['message']

    # Returns whether the credentials are known to the printer. They may not be if the printer was reset.
    # Note that this is completely different from get_auth_check.
    def get_auth_verify(self) -> bool:
        return requests.get(
            url=f"http://{self.host}/api/v1/auth/verify", auth=HTTPDigestAuth(self.credentials.id, self.credentials.key), timeout=self.timeout).status_code != 401

    def get_printer_status(self) -> str:
        return requests.get(
            url=f"http://{self.host}/api/v1/printer/status", auth=self.digest_auth(), timeout=self.timeout).json()

    def get_print_job(self) -> PrintJob:
        print_job_dict: Dict = requests.get(url=f"http://{self.host}/api/v1/print_job", auth=self.digest_auth(), timeout=self.timeout).json()
        return PrintJob.parse(print_job_dict)

    def get_print_job_state(self) -> str:
        return requests.get(
            url=f"http://{self.host}/api/v1/print_job/state", auth=self.digest_auth(), timeout=self.timeout).json()

    def get_print_job_time_elapsed(self) -> datetime.timedelta:
        return datetime.timedelta(seconds=requests.get(
            url=f"http://{self.host}/api/v1/print_job/time_elapsed", auth=self.digest_auth(), timeout=self.timeout).json())

    def get_print_job_time_total(self) -> datetime.timedelta:
        return datetime.timedelta(seconds=requests.get(
            url=f"http://{self.host}/api/v1/print_job/time_total", auth=self.digest_auth(), timeout=self.timeout).json())

    def get_print_job_progress(self) -> float:
        return requests.get(
            url=f"http://{self.host}/api/v1/print_job/progress", auth=self.digest_auth(), timeout=self.timeout).json()

    def get_print_job_name(self) -> str:
        return requests.get(
            url=f"http://{self.host}/api/v1/print_job/name", auth=self.digest_auth(), timeout=self.timeout).json()

    def put_system_display_message(self, message: str, button_caption: str) -> str:
        return requests.put(url=f"http://{self.host}/api/v1/system/display_message", auth=self.digest_auth(), json={'message': message, 'button_caption': button_caption}, timeout=self.timeout).json()

    # Frequency in Hz, duration in ms
    def put_beep(self, frequency: float, duration: float) -> str:
        return requests.put(url=f"http://{self.host}/api/v1/beep", auth=self.digest_auth(), json={'frequency': frequency, 'duration': duration}, timeout=self.timeout).json()

    def get_system_guid(self) -> UUID:
        if self.guid is None:
            self.guid = UUID(requests.get(url=f'http://{self.host}/api/v1/system/guid', timeout=self.timeout).json())
        return self.guid

    def get_system_name(self) -> str:
        self.name = requests.get(url=f'http://{self.host}/api/v1/system/name', timeout=self.timeout).json()
        return self.name

    def get_camera_snapshot_uri(self) -> str:
        res: requests.Response = requests.get(url=f'http://{self.address}:8080/?action=snapshot', timeout=self.timeout)
        image: Image = Image.open(io.BytesIO(res.content))
        hash: imagehash.ImageHash = imagehash.phash(image)
        if self.camera_snapshot_uri is None or hash != self.camera_snapshot_uri[1]:
            self.camera_snapshot_uri = (f"data:{res.headers['Content-Type']};base64,{base64.b64encode(res.content).decode('utf-8')}", hash)
        return self.camera_snapshot_uri[0]
