2026-08-17T12:25:36.161603254Z     from market_data_router import (
2026-08-17T12:25:36.161608104Z   File "/app/market_data_router.py", line 16, in <module>
2026-08-17T12:25:36.161665437Z     from ig_demo_broker import IGDemoBroker, IGDemoError
2026-08-17T12:25:36.161668947Z   File "/app/ig_demo_broker.py", line 12, in <module>
2026-08-17T12:25:36.16174683Z     from ig_demo_broker import IGDemoBroker, IGDemoError
2026-08-17T12:25:36.161750481Z ImportError: cannot import name 'IGDemoBroker' from partially initialized module 'ig_demo_broker' (most likely due to a circular import) (/app/ig_demo_broker.py)
2026-08-17T12:25:37.495211802Z ==> Exited with status 1
2026-08-17T12:25:37.497606443Z ==> Common ways to troubleshoot your deploy: https://render.com/docs/troubleshooting-deploys
2026-08-17T12:25:38.386440058Z Traceback (most recent call last):
2026-08-17T12:25:38.38649883Z   File "/usr/local/bin/uvicorn", line 8, in <module>
2026-08-17T12:25:38.38651347Z     sys.exit(main())
2026-08-17T12:25:38.386516401Z              ^^^^^^
2026-08-17T12:25:38.386519521Z   File "/usr/local/lib/python3.11/site-packages/click/core.py", line 1569, in __call__
2026-08-17T12:25:38.386756281Z     return self.main(*args, **kwargs)
2026-08-17T12:25:38.386764731Z            ^^^^^^^^^^^^^^^^^^^^^^^^^^
2026-08-17T12:25:38.386769621Z   File "/usr/local/lib/python3.11/site-packages/click/core.py", line 1490, in main
2026-08-17T12:25:38.386998491Z     rv = self.invoke(ctx)
2026-08-17T12:25:38.387002271Z          ^^^^^^^^^^^^^^^^
2026-08-17T12:25:38.387005601Z   File "/usr/local/lib/python3.11/site-packages/click/core.py", line 1353, in invoke
2026-08-17T12:25:38.387197489Z     return ctx.invoke(self.callback, **ctx.params)
2026-08-17T12:25:38.38720265Z            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
2026-08-17T12:25:38.38720544Z   File "/usr/local/lib/python3.11/site-packages/click/core.py", line 907, in invoke
2026-08-17T12:25:38.387361956Z     return callback(*args, **kwargs)
2026-08-17T12:25:38.387365646Z            ^^^^^^^^^^^^^^^^^^^^^^^^^
2026-08-17T12:25:38.387368186Z   File "/usr/local/lib/python3.11/site-packages/uvicorn/main.py", line 440, in main
2026-08-17T12:25:38.387497172Z     run(
2026-08-17T12:25:38.387504502Z   File "/usr/local/lib/python3.11/site-packages/uvicorn/main.py", line 609, in run
2026-08-17T12:25:38.387614847Z     config.load_app()
2026-08-17T12:25:38.387618537Z   File "/usr/local/lib/python3.11/site-packages/uvicorn/config.py", line 428, in load_app
2026-08-17T12:25:38.387719521Z     return import_from_string(self.app)
2026-08-17T12:25:38.387722881Z            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^
2026-08-17T12:25:38.387729522Z   File "/usr/local/lib/python3.11/site-packages/uvicorn/importer.py", line 19, in import_from_string
2026-08-17T12:25:38.387795135Z     module = importlib.import_module(module_str)
2026-08-17T12:25:38.387798215Z              ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
2026-08-17T12:25:38.387800695Z   File "/usr/local/lib/python3.11/importlib/__init__.py", line 126, in import_module
2026-08-17T12:25:38.387889238Z     return _bootstrap._gcd_import(name[level:], package, level)
2026-08-17T12:25:38.387892419Z            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
2026-08-17T12:25:38.387895609Z   File "<frozen importlib._bootstrap>", line 1204, in _gcd_import
2026-08-17T12:25:38.387898059Z   File "<frozen importlib._bootstrap>", line 1176, in _find_and_load
2026-08-17T12:25:38.387907189Z   File "<frozen importlib._bootstrap>", line 1147, in _find_and_load_unlocked
2026-08-17T12:25:38.387909759Z   File "<frozen importlib._bootstrap>", line 690, in _load_unlocked
2026-08-17T12:25:38.387912109Z   File "<frozen importlib._bootstrap_external>", line 940, in exec_module
2026-08-17T12:25:38.38791452Z   File "<frozen importlib._bootstrap>", line 241, in _call_with_frames_removed
2026-08-17T12:25:38.38791708Z   File "/app/main.py", line 13, in <module>
2026-08-17T12:25:38.387982062Z     from market_data_router import (
2026-08-17T12:25:38.387987483Z   File "/app/market_data_router.py", line 16, in <module>
2026-08-17T12:25:38.388024444Z     from ig_demo_broker import IGDemoBroker, IGDemoError
2026-08-17T12:25:38.388027894Z   File "/app/ig_demo_broker.py", line 12, in <module>
2026-08-17T12:25:38.388090047Z     from ig_demo_broker import IGDemoBroker, IGDemoError
2026-08-17T12:25:38.388095857Z ImportError: cannot import name 'IGDemoBroker' from partially initialized module 'ig_demo_broker' (most likely due to a circular import) (/app/ig_demo_broker.py)
