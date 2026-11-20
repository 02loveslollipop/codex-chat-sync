.PHONY: test install update status

PYTHON ?= python3

test:
	PYTHONPATH=packages:installer $(PYTHON) -m unittest discover -s tests -v

install:
	bash ./install.sh

update:
	bash ./install.sh --update

status:
	bash ./install.sh --status
