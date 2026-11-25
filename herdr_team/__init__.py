import sys

# Every entry point (bin/herdr-team, the plugin's actions and panes) runs `python3 -m herdr_team`,
# which imports this first. Keep this file parseable by old Pythons.


def version_error(v=sys.version_info):
    if tuple(v[:2]) < (3, 11):
        return "herdr-team needs Python 3.11+ (found %d.%d)" % (v[0], v[1])
    return None


if version_error():
    sys.exit(version_error())
