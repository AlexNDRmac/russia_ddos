import os
import time
import hashlib
from datetime import datetime
from collections import defaultdict
from typing import Tuple

from rich.console import Console

from ripper import common
from ripper.common import is_ipv4, strip_lines
from ripper.constants import DEFAULT_CURRENT_IP_VALUE, MIN_SCREEN_WIDTH
from ripper.proxy_manager import ProxyManager
from ripper.socket_manager import SocketManager
from ripper.proxy import read_proxy_list, Sock5Proxy


def get_headers_dict(base_headers: list[str]):
    """Set headers for the request"""
    headers_dict = {}
    for line in base_headers:
        parts = line.split(':')
        headers_dict[parts[0]] = parts[1].strip()

    return headers_dict


class PacketsStats:
    """Class for TCP/UDP statistic collection."""
    total_sent: int = 0
    """Total packets sent by TCP/UDP."""
    total_sent_prev: int = 0
    """Total packets sent by TCP/UDP (previous state)"""
    total_sent_bytes: int = 0
    """Total sent bytes by TCP/UDP connect."""
    connections_check_time: int = 0
    """Connection last check time."""

    def sync_packets_sent(self):
        """Sync previous packets sent stats with current packets sent stats."""
        self.total_sent_prev = self.total_sent

    def status_sent(self, sent_bytes: int = 0):
        """
        Collect sent packets statistic.
        :param sent_bytes sent packet size in bytes
        """
        self.total_sent += 1
        self.total_sent_bytes += sent_bytes


class ConnectionStats:
    """Class for Connection statistic"""
    success_prev: int = 0
    """Total connections to HOST with Success status (previous state)"""
    success: int = 0
    """Total connections to HOST with Success status"""
    failed: int = 0
    """Total connections to HOST with Failed status."""
    last_check_time: int = 0
    """Last check connection time."""
    in_progress: bool = False
    """Connection state used for checking liveness of Socket."""

    def get_success_rate(self) -> int:
        """Calculate Success Rate for connection."""
        if self.success == 0:
            return 0

        return int(self.success / (self.success + self.failed) * 100)

    def sync_success(self):
        """Sync previous success state with current success state."""
        self.success_prev = self.success

    def set_state_in_progress(self):
        """Set connection State - in progress."""
        self.in_progress = True

    def set_state_is_connected(self):
        """Set connection State - is connected."""
        self.in_progress = False

    def status_success(self):
        """Collect successful connections."""
        self.success += 1

    def status_failed(self):
        """Collect failed connections."""
        self.failed += 1


class Statistic:
    """Collect all statistics."""
    packets: PacketsStats = PacketsStats()
    """Collect all the stats about TCP/UDP and HTTP packets."""
    http_stats = defaultdict(int)
    """Collect stats about HTTP response codes."""
    connect: ConnectionStats = ConnectionStats()
    """Collect all the Connections stats via Socket or HTTP Client."""
    start_time: datetime = None
    """Script start time."""

    def collect_packets_success(self, sent_bytes: int = 0):
        self.packets.total_sent += 1
        self.packets.total_sent_bytes += sent_bytes


class IpInfo:
    """All the info about IP addresses and Geo info."""
    my_country: str = None
    """Country code based on your public IPv4 address."""
    target_country: str = None
    """Country code based on target public IPv4 address."""
    my_start_ip: str = None
    """My IPv4 address within script starting."""
    my_current_ip: str = None
    """My current IPv4 address. It can be changed during script run."""
    isCloudflareProtected: bool = False
    """Is Host protected by CloudFlare."""

    def cloudflare_status(self) -> str:
        """Get human-readable status for CloudFlare target HOST protection."""
        return 'Protected' if self.isCloudflareProtected else 'Not protected'

    def my_ip_masked(self) -> str:
        """
        Get my initial IPv4 address with masked octets.

        127.0.0.1 -> 127.*.*.*
        """
        parts = self.my_start_ip.split('.')
        if not parts[0].isdigit():
            return DEFAULT_CURRENT_IP_VALUE

        if len(parts) > 1 and parts[0].isdigit():
            return f'{parts[0]}.*.*.*'
        else:
            return parts[0]


class Errors:
    """Class with Error details."""
    uuid: str = None
    """UUID for Error, based on error details."""
    time: datetime = None
    """Error time."""
    code: str = None
    """Error type or process/operation short name"""
    count: int = 0
    """Error count."""
    message: str = ''
    """Error message"""

    def __init__(self, code: str, message: str, count: int = 1):
        """
        :param code: Error type
        :param message: Error message
        :param count: Error counter
        """
        self.uuid = hashlib.sha1(f'{code}{message}'.encode()).hexdigest()
        self.time = datetime.now()
        self.code = code
        self.count = count
        self.message = message


class Context:
    """Class (Singleton) for passing a context to a parallel processes."""

    # ==== Input params ====
    host: str = ''
    """Original HOST name from input args. Can be domain name or IP address."""
    host_ip: str = ''
    """HOST IPv4 address."""
    port: int = 80
    """Destination Port."""
    threads: int = 100
    """The number of threads."""
    max_random_packet_len: int = 48
    """Limit for Random Packet Length."""
    random_packet_len: bool = False
    """Is Random Packet Length enabled."""
    attack_method: str = None
    """Current attack method."""
    proxy_list: list[Sock5Proxy]
    """File with proxies in ip:port:username:password or ip:port line format."""
    proxy_type: str
    """Type of proxy to work with. Supported types: socks5, socks4, http."""
    http_method: str
    """HTTP method used in HTTP packets"""
    http_path: str
    """HTTP path used in HTTP packets"""

    # ==== Statistic ====
    Statistic: Statistic = Statistic()
    """All the statistics collected separately by protocols and operations."""
    IpInfo: IpInfo = IpInfo()
    """All the info about IP addresses and GeoIP information."""
    errors: dict[str, Errors] = defaultdict(dict[str, Errors])
    """All the errors during script run."""

    # ==========================================================================

    cpu_count: int = 1
    """vCPU cont of current machine."""
    protocol: str = 'http://'
    """HTTP protocol. Can be http/https."""

    # ==== Internal variables ====
    user_agents: list = None
    """User-Agent list. RAW data for random choice."""
    base_headers: list = None
    """Base HTTP headers list for HTTP Client. RAW data for internal usage."""
    headers: dict[str, str] = defaultdict(dict[str, str])
    """HTTP Headers used to make Requests."""

    # External API and services info
    sock_manager: SocketManager = SocketManager()
    proxy_manager: ProxyManager = ProxyManager()
    logger: Console = Console(width=MIN_SCREEN_WIDTH)

    # Health-check
    is_health_check: bool = True
    """Controls health check availability. Turn on: 1, turn off: 0."""
    connections_check_time: int = 0
    fetching_host_statuses_in_progress: bool = False
    last_host_statuses_update: datetime = None
    health_check_method: str = ''
    host_statuses = {}

    target: Tuple[str, int]

    def get_target_url(self) -> str:
        """Get fully qualified URI for target HOST - schema://host:port"""
        return f"{self.protocol}{self.host}:{self.port}{self.http_path}"

    def get_start_time_ns(self) -> int:
        """Get start time in nanoseconds."""
        if not self.Statistic.start_time:
            return 0
        return int(self.Statistic.start_time.timestamp() * 1000000 * 1000)

    def add_error(self, error: Errors):
        """
        Add Error to Errors collection without duplication.
        If Error exists in collection - it updates the error counter.
        """
        if self.errors.__contains__(error.uuid):
            self.errors[error.uuid].count += 1
            self.errors[error.uuid].time = error.time
        else:
            self.errors[error.uuid] = error

    def remove_error(self, error_code: str):
        """Remove Error from collection by Error Code."""
        if self.errors.__contains__(error_code):
            self.errors.__delitem__(error_code)

    def has_errors(self) -> bool:
        """Check if Errors are exists."""
        return len(self.errors) > 0

    def validate(self):
        """Validates context before Run script. Order is matter!"""
        if self.host_ip is None or not is_ipv4(self.host_ip):
            self.logger.log(f'Cannot get IPv4 for HOST: {self.host}. Could not connect to the target HOST.')
            exit(1)

        if self.IpInfo.my_start_ip is None or not is_ipv4(self.IpInfo.my_start_ip):
            self.logger.log(f'Cannot get your public IPv4 address. Check your VPN connection.')
            exit(1)

    def __new__(cls, args):
        """Singleton realization."""
        if not hasattr(cls, 'instance'):
            cls.instance = super().__new__(cls)
        return cls.instance

    def __init__(self, args):
        self.host = getattr(args, 'host', '')
        self.port = getattr(args, 'port', 80)
        self.threads = getattr(args, 'threads', 100)
        self.attack_method = getattr(args, 'attack_method', 'tcp').lower()
        self.random_packet_len = bool(getattr(args, 'random_packet_len', 0))
        self.max_random_packet_len = int(getattr(args, 'max_random_packet_length', 0))
        self.is_health_check = bool(getattr(args, 'health_check', 0))
        self.http_method = getattr(args, 'http_method', 'GET').upper()
        self.http_path = getattr(args, 'http_path', '/').lower()
        self.sock_manager.socket_timeout = getattr(args, 'socket_timeout', 10)

        self.host_ip = common.get_ipv4(self.host)
        self.protocol = 'https://' if self.port == 443 else 'http://'

        if self.random_packet_len and not self.max_random_packet_len:
            self.max_random_packet_len = int(getattr(args, 'max_random_packet_len', 1))
        elif self.attack_method == 'http':
            self.random_packet_len = False
            self.max_random_packet_len = 0
        elif self.attack_method == 'tcp':
            self.max_random_packet_len = 1024

        self.target = (self.host_ip, self.port)

        self.cpu_count = max(os.cpu_count(), 1)  # to avoid situation when vCPU might be 0

        # Get required data from files
        self.user_agents = strip_lines(common.readfile(os.path.dirname(__file__) + '/useragents.txt'))
        self.base_headers = strip_lines(common.readfile(os.path.dirname(__file__) + '/headers.txt'))
        self.headers = get_headers_dict(self.base_headers)

        # Get initial info from external services
        self.IpInfo.my_start_ip = common.get_current_ip()
        self.IpInfo.my_current_ip = self.IpInfo.my_start_ip
        self.IpInfo.my_country = common.get_country_by_ipv4(self.IpInfo.my_start_ip)
        self.IpInfo.target_country = common.get_country_by_ipv4(self.host_ip)
        self.IpInfo.isCloudflareProtected = common.isCloudFlareProtected(self.host, self.user_agents)

        self.Statistic.start_time = datetime.now()
        self.connections_check_time = time.time_ns()
        self.health_check_method = 'ping' if self.attack_method == 'udp' else self.attack_method

        if getattr(args, 'proxy_list', False) and self.attack_method != 'udp':
            self.proxy_list = read_proxy_list(getattr(args, 'proxy_list', ''))
            self.proxy_manager.set_proxy_list(self.proxy_list)

        # proxies are slower, so wee needs to increase timeouts 2x times
        if self.proxy_manager.proxy_list_initial_len:
            self.sock_manager.socket_timeout *= 2
