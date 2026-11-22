# Contributing to saleslop

Run these commands from the checkout in a Python 3.11+ virtual environment:

```sh
python -m pip install -r install/requirements.txt
python -m unittest discover -s tests -v
```

The tests use temporary directories, fixture addresses and local stand-ins for sales services. The dependencies enable HTTP adapter tests and provide timezone data on Windows. A quick offline check of the starter pack is `python runner/kit_env.py --pack config/pack.template.json check`.

For a bug report, include the command, Python version, expected result and observed result. Replace contact details and customer material with synthetic inputs. Keep team packs, logs, exports and account credentials in a separate directory outside the checkout.

For a change to a provider or a send path, include an offline test that shows what happens when the provider refuses or the result is uncertain. Describe any checks that still require a live account.
