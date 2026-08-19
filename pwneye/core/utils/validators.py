import ipaddress
import re

def validate_ip_or_domain(value: str) -> str:
    """
    Validate that the provided value is a valid IP address, fully qualified domain name (FQDN),
    or short hostname. Trailing dots are not allowed.

    A value shaped like a dotted-decimal IPv4 address (``a.b.c.d``) is validated
    strictly with :mod:`ipaddress`, so out-of-range octets such as
    ``999.999.999.999`` are rejected instead of slipping through as
    hostname-shaped input. Genuine hostnames remain leniently accepted; real
    resolution still happens at connect time.

    :param value: The input string to validate.
    :return: The validated IP or domain.
    """
    hostname_regex = re.compile(
        r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.[A-Za-z0-9-]{1,63})*$"
    )
    dotted_quad_regex = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")

    if value.endswith("."):
        raise ValueError("Trailing dots are not allowed.")

    if dotted_quad_regex.match(value):
        try:
            ipaddress.IPv4Address(value)
        except ValueError:
            raise ValueError("Must be a valid IP address or hostname.")
        return value

    if hostname_regex.match(value):
        return value

    raise ValueError("Must be a valid IP address or hostname.")

def validate_port(value: str) -> int:
    """
    Validate that the provided value is a valid TCP port.

    :param value: The input string to validate.
    :return: The validated port as an integer.
    """
    try:
        port = int(value)
    except ValueError:
        raise ValueError("must be an integer between 1 and 65535")

    if 1 <= port <= 65535:
        return port

    raise ValueError("must be an integer between 1 and 65535")

def validate_timeout(value: str) -> int:
    """
    Validate that the provided value is a positive connection timeout in seconds.
    """
    try:
        timeout = int(value)
    except ValueError:
        raise ValueError("must be a positive integer number of seconds")

    if timeout <= 0:
        raise ValueError("must be greater than 0")

    return timeout


def validate_threads(value: str) -> int:
    """
    Validate that the provided value is a positive thread count.
    """
    try:
        threads = int(value)
    except ValueError:
        raise ValueError("must be a positive integer")

    if threads < 1:
        raise ValueError("must be at least 1")

    return threads


def validate_max_channels(value: str) -> int:
    """
    Validate that the provided value is a positive channel-enumeration cap.
    """
    try:
        maximum = int(value)
    except ValueError:
        raise ValueError("must be a positive integer")

    if maximum < 1:
        raise ValueError("must be at least 1")

    return maximum
