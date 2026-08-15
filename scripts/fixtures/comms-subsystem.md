# Orbital Platform — Communications Subsystem Briefing

## Link architecture

The platform operates three independent downlink paths. They are deliberately not
interchangeable: each has a different availability profile and a different failure mode.

The **Ka-band high-rate downlink** carries science data through a steerable 1.2 m dish on the
zenith truss. It is budgeted at **220 Mbps** but achieves an operational average of
**164 Mbps** because the relay constellation is visible for only 74% of each orbit and the
link margin degrades sharply below fifteen degrees elevation.

The **S-band command and telemetry link** carries housekeeping data and all uplinked
commands. It runs at **2.1 Mbps** downlink and **256 kbps** uplink, through two omni
antennas that give continuous coverage with no pointing requirement. This is the link that
must never fail, which is why it is the slowest and the simplest.

The **UHF proximity link** handles traffic to visiting vehicles within 200 km. It runs at
**1.5 Mbps** and is active only during rendezvous and departure windows.

## Data volumes

Science instruments generate approximately **1.9 TB per day**. The Ka-band link at its
operational average clears about **1.48 TB per day**, so a backlog accumulates during high
duty-cycle campaigns and is drained during quiet periods. Onboard storage is **34 TB**,
which is roughly eighteen days of unclearable backlog before the oldest data is overwritten.

Housekeeping telemetry is 42 GB per day and is never backlogged.

## Power draw

The communications allocation is 4.2 kW, split as follows: the Ka-band transmitter and its
pointing mechanism draw 2.9 kW while transmitting, the S-band chain draws 0.8 kW
continuously, and the UHF link draws 0.5 kW when active. Because the Ka-band link is only
transmitting for 74% of each orbit, the measured average communications draw is closer to
**3.5 kW** than to the 4.2 kW allocation.

During a Tier 2 load shed the Ka-band transmitter is powered down entirely and the
allocation falls to the S-band chain alone.

## Ka-band availability

Relay visibility is not uniform across the orbit. The link is unavailable in three arcs per
orbit, of approximately eight, six and ten minutes, corresponding to relay handover gaps and
one geometric occultation by the platform's own radiator boom.

The boom occultation is the only one that could be engineered away, and it was accepted at
design time because moving the dish would have required lengthening the zenith truss.

## What this briefing does not cover

The modulation and coding schemes, the encryption architecture, and the ground segment
scheduling process are held in separate documents. The propellant cost of the attitude
adjustments required for dish pointing is covered in the propulsion briefing, not here.
