# Project-owned agent skills

This directory contains four agent skills maintained and released by
the AI Mechanical 3DCAD Design Agent project:

- `mechanical-design`
- `freecad-standard-parts`
- `freecad-model-validation`
- `mechanical-fea`

The installed wheel bundles `mechanical-fea`; export it to a new project skill
directory with `mechanical-design analysis skill --destination <directory>`.

Each skill is self-contained under its own directory. External optional
workflows, including Superpowers brainstorming, are documented but are not
vendored or installed by this repository.
