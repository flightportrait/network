"""The fixed Pacific oceanic routes, from refdata/pac_airways.json.

The North Pacific routes (NOPAC: R220, R580, A590, ...) join Alaska to
Japan's airspace at 160 degrees east; the Central East Pacific routes
(CEPAC: R576, R577, R578, ...) join Hawaii to the mainland coast. They
are fixed airways, flown either way depending on the route and the
hour, and published by the FAA (tools/pac_airways.py builds the file
from the FAA's NASR data). The daily Pacific tracks (PACOTS) are not
here: they need the FAA's NOTAM feed.
"""
import json
import os

AIRWAYS_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "refdata", "pac_airways.json")
_AIRWAYS = {}


def airways():
    """{airway: [(lat, lon), ...]} in published order."""
    if not _AIRWAYS:
        with open(AIRWAYS_JSON) as fh:
            for name, pts in json.load(fh).items():
                _AIRWAYS[name] = [(p[1], p[2]) for p in pts]
    return _AIRWAYS
