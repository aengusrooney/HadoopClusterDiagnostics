#!/bin/bash

#
# Run this from an hdp repo root for any component. Example:
#
# git clone git@github.com:hortonworks/hbase.git 
# cd hbase 
# find_hdp_commit.sh HBASE-14759
#

if [ "$#" -eq "0" ] ; then
  echo "usage: $0 <commit_text> "
  exit
fi

COMMIT_TEXT=$1


HDP_VERSIONS="2.1 2.2 2.3 2.4"
HDP_BRANCHES="2.1-maint 2.2-maint 2.3-maint"

echo
echo "****************************************************"
echo "Git fetch -t";
echo "****************************************************"

git fetch -t 

echo
echo "****************************************************"
echo "Greping commit ($COMMIT_TEXT) for HDP releases 2.1+";
echo "****************************************************"

for HDP_VERSION in $HDP_VERSIONS; do
  TAGS=`git tag | grep "HDP-$HDP_VERSION" | grep "tag"`

  for TAG in $TAGS; do
    echo "$TAG:"
    git log $TAG | grep $COMMIT_TEXT
  done
done

echo
echo "****************************************************"
echo "Greping commit ($COMMIT_TEXT) for HDP branches 2.1+";
echo "****************************************************"
for BRANCH in $HDP_BRANCHES; do
  echo "$BRANCH:"
  git log origin/$BRANCH | grep $COMMIT_TEXT
done
