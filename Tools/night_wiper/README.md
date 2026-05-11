USAGE:
night_wiper.py --lights-off || --lights-on --theater THEATER-DIR save.cam

`--theater` is required. Pass the theater `Data` directory, for example the
main KTO `Data` directory or an add-on theater directory such as
`Data\Add-On UOAF 80s`.

night_wiper.ini format:

```
[night airframes]
F-111
F-4

[protected squadrons]
camp_id:1715
unit_id:8690
unit_id:8690,0
name_id:13
aircraft:F-16CM-52
```

Example:
```
python night_wiper.py --lights-off --theater D:\Falcon BMS 4.38\Data D:\Falcon BMS 4.38\Data\Campaign\777pre.cam

python night_wiper.py --lights-on --theater D:\Falcon BMS 4.38\Data\Add-On UOAF 80s D:\Falcon BMS 4.38\Data\Campaign\Add-On UOAF 80s\fnf1pre.cam
```

What will happen:
on --lights-off: 
1) the ATO will be wiped for all flights, except for the ones belonging to [protected squadrons];
2) all squadrons, except for [night airframes] and [protected squadrons] will flip to human control.
on --lights-on:
all squadrons, except for [protected squadrons], will flip back to HQ control.


The app edits `save.cam` in-place after creating a timestamped backup next to it.

For now, human control only means the inferred `unit_flags` bit `0x80`. Squadron
`specialty` / `Set by HQ` is not changed.
