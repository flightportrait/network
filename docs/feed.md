# Feed the network

One question decides the path: do you already have a receiver?

## Yes: add one line

Feeding is one line in a receiver you already run. It is not
exclusive; keep feeding the others.

readsb, in `/etc/default/readsb`, `NET_OPTIONS`:

```
--net-connector feed.flightportrait.com,30004,beast_reduce_plus_out,uuid=YOUR-KEY
```

ultrafeeder, in `ULTRAFEEDER_CONFIG`:

```
adsb,feed.flightportrait.com,30004,beast_reduce_plus_out,uuid=YOUR-KEY;mlat,feed.flightportrait.com,31090,uuid=YOUR-KEY
```

dump1090-fa 6.0 or newer takes the readsb form in
`/etc/default/dump1090-fa`. Images with an aggregator list (adsb.im and
others): under custom aggregators, host `feed.flightportrait.com`, port
`30004`, protocol `beast_reduce_plus_out`, your key. We are asking to be
added to the built-in lists.

The key is a UUID. [The feed page](https://flightportrait.com/network/?mode=join&door=yes)
issues one and watches until the station is heard, or make one with
`cat /proc/sys/kernel/random/uuid`. Keep it: it is the station identity
and opens its status page. We store a hash, not the key.
[Privacy](privacy.md).

## No: the Station

A Raspberry Pi (3B or newer, or a Zero 2 W), a USB receiver for 1090
MHz (one with a built-in filter, like the FlightAware Pro Stick Plus,
hears furthest; any RTL-SDR dongle works), a 1090 MHz antenna, a power
supply and a microSD card; about US$100, no soldering. The antenna's
place matters most: high, by a window or outside, open sky around it.

Flash **Raspberry Pi OS Lite (64-bit)** with Raspberry Pi Imager (the
Station runs on 64-bit only), naming the Pi `station` and turning on
SSH and your Wi-Fi in its settings. Log in from a computer on the same
Wi-Fi (`ssh username@station.local`), run one line (published with the
Station release), and answer the setup in your browser at
`station.local:8654`: a name, the antenna on a map, which networks to
feed. The setup shows the station key; paste it on
[the feed page](https://flightportrait.com/network/?mode=join&door=no)
to watch for the station.

Already have a receiver and want the Station instead? A dongle serves
one program, so the Station replaces what runs today; the installer
finds the receiver first, imports feeds and keys, and asks before it
stops anything (`install.sh --replace`).

## Returning

Have a key already? Use it. It keeps your station's history; a new
key is a new station.

## What is public

The roster shows a generated id, online status, and a location
rounded to about 11 km from coverage. Not an address. Feeder IPs
are not stored.

[Feeder terms](https://flightportrait.com/network/terms): you keep
your data; the aggregate is published under ODbL. Stop whenever you
like.
