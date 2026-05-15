#!/bin/bash
cd /opt/smart-home-git
git pull origin master
git add .
git commit -m "backup: $(date +%d%m%Y_%H%M%S)"
git push origin master