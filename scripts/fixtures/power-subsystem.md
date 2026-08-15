# Orbital Platform — Power Subsystem Briefing

## Generation

The platform carries four deployable solar arrays. Each array is rated at **9.4 kW** at
beginning of life, giving a nominal generation capacity of **37.6 kW**. Array output decays
at roughly 1.8% per year from radiation damage and micrometeoroid abrasion, so the design
end-of-life figure after the fifteen-year service life is **28.7 kW**.

Two of the four arrays are on the forward truss and two on the aft truss. The forward pair
is shadowed by the radiator boom for approximately eleven minutes of each ninety-two minute
orbit, which is why the published average generation figure of **31.2 kW** is lower than a
naive sun-fraction calculation suggests.

## Storage

Energy storage is provided by twenty-four lithium-ion battery modules arranged in six
strings of four. Each module holds **4.1 kWh** of usable capacity, for a total usable store
of **98.4 kWh**. The batteries are cycled once per orbit and are sized for a maximum depth
of discharge of 35%, which is what gives them their eight-year replacement interval.

Battery replacement is a scheduled extravehicular activity. Two strings are replaced per
maintenance window and there are three windows per year.

## Allocation by subsystem

The steady-state load is allocated as follows. These figures are the design allocation, not
measured draw, and the difference matters when reading telemetry.

| Subsystem | Allocation |
|---|---|
| Life support and thermal | 11.8 kW |
| Science payloads | 8.9 kW |
| Communications | 4.2 kW |
| Attitude control and reaction wheels | 3.1 kW |
| Command, data handling and avionics | 2.4 kW |
| Lighting and crew systems | 1.6 kW |

That totals **32.0 kW** against an average generation of 31.2 kW. The deficit is covered by
the battery store and is the reason science payloads are the first load shed during a
conjunction manoeuvre.

## Load shedding

Three shed tiers are defined. Tier 1 sheds science payloads and recovers 8.9 kW. Tier 2
additionally sheds non-essential lighting and half the communications allocation, recovering
a further 3.7 kW. Tier 3 is a survival configuration retaining only life support, avionics
and the emergency beacon, and draws **6.8 kW**.

Tier 3 has been entered twice in the platform's operational history, both times during
solar particle events rather than hardware faults.

## What this briefing does not cover

The thermal rejection budget, the details of the reaction wheel desaturation schedule, and
the propellant accounting for conjunction manoeuvres are all held in separate documents.
