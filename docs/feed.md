# Feed the network

Feeding is one line in a receiver you may already run. It is not
exclusive.

## Already feeding

```
adsb,feed.flightportrait.com,30004,beast_reduce_plus_out,uuid=YOUR-UUID
```

Generate a UUID with `cat /proc/sys/kernel/random/uuid` and keep it.
It is the station identity and the key to its status page. We store
a hash, not the UUID. [Privacy](privacy.md).

## Starting from zero

A receiver is an RTL-SDR dongle with a 1090 MHz antenna (about
US$40) and a computer that runs Docker, including a Raspberry Pi 2
through 5.

[The join page](https://flightportrait.com/network/?mode=join)
issues the UUID and watches until the station is heard.

## What is public

The roster shows a generated id, online status, and a location
rounded to about 11 km from coverage. Not an address. Feeder IPs
are not stored.

[Feeder terms](https://flightportrait.com/network/terms): you keep
your data; the aggregate is published under ODbL. Stop whenever you
like.
