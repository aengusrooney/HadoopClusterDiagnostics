#!/bin/env bash

# A. Run the hdfs_template.sh on "Active NameNode, StandBy NameNode, JournalNode, DataNode"  PIDs on the respective nodes. 
# B. Collect following:
# 
# 1.    Collect the GC logs for Active NameNode, StandBy NameNode, JournalNode and ZKFC. 
# 2.    Collect the "namenode.out" file.
# 3.    Collect the "Hadoop configuration files"
#           /etc/hadoop/conf/*
# 4.    Collect Zookeeper Logs (from all the Zookeeper nodes).
# 5.    Collect JournalNode Logs (from all the Journal nodes).

function syschk() {
    local __doc__="Execute OS command to check system for triage"
    local _p="$1"	# Java PID ex: `cat /var/run/hadoop/hdfs/hadoop-hdfs-journalnode.pid`, `cat /var/run/hadoop/hdfs/hadoop-hdfs-datanode.pid`
    local _work_dir="$2"
    [ -z "${_work_dir}" ] && _work_dir="/hadoop/${FUNCNAME}_tmp_dir"
    if [ ! -d "$_work_dir" ] && ! mkdir $_work_dir; then
        echo "ERROR: Couldn't create $_work_dir directory"; return 1
    fi
    
    chmod 777 $_work_dir
    
    if [ -n "$_p" ]; then
        echo "Collecting java PID $_p related information..."
        local _j="$(dirname `readlink /proc/${_p}/exe`)" 2>/dev/null
        if [ -n "$_j" ]; then
            local _u="`stat -c '%U' /proc/${_p}`"
            for i in {1..3};do su ${_u} -c "jstack -l ${_p}"; sleep 5; done &> ${_work_dir%/}/jstack_${_p}.out &
            su ${_u} -c "jstat -gccause ${_p} 1000 5" &> ${_work_dir%/}/jstat_${_p}.out &
            su ${_u} -c "jmap -histo:live ${_p}" &> ${_work_dir%/}/jmap_histo_${_p}.out &
        fi
        ps -eLo user,pid,lwp,nlwp,ruser,pcpu,stime,etime,args | grep -w "${_p}" &> ${_work_dir%/}/pseLo_${_p}.out
        cat /proc/${_p}/limits &> ${_work_dir%/}/proc_limits_${_p}.out
        cat /proc/${_p}/status &> ${_work_dir%/}/proc_status_${_p}.out
        cat /proc/${_p}/io &> ${_work_dir%/}/proc_io_${_p}.out;sleep 5;cat /proc/${_p}/io >> ${_work_dir%/}/proc_io_${_p}.out
        cat /proc/${_p}/environ | tr '\0' '\n' > ${_work_dir%/}/proc_environ_${_p}.out
        jstat -gc ${_p} |tail -n 1| awk '{split($0,a," "); sum=a[3]+a[4]+a[6]+a[8]; print sum}' > ${_work_dir%/}/heap_usedby_${_p}.out
        pmap -x ${_p} &> ${_work_dir%/}/pmap_${_p}.out
    fi

    echo "Collecting OS related information..."
    vmstat 1 3 &> ${_work_dir%/}/vmstat.out &
    pidstat -dl 3 3 &> ${_work_dir%/}/pstat.out &
    sysctl -a &> ${_work_dir%/}/sysctl.out
    top -b -n 1 -c &> ${_work_dir%/}/top.out
    ps auxwwwf &> ${_work_dir%/}/ps.out
    netstat -aopen &> ${_work_dir%/}/netstat.out
    netstat -i &> ${_work_dir%/}/netstat_i.out
    netstat -s &> ${_work_dir%/}/netstat_s.out
    ifconfig &> ${_work_dir%/}/ifconfig.out
    set -x
    su ${_u} -c "jmap -dump:live,format=b,file=${_work_dir%/}/jmap_live_dump_${_p}.out ${_p}"
    nscd -g &> ${_work_dir%/}/nscd.out
    wait
    echo "Creating tar.gz file..."
    local _file_path="./chksys_$(hostname)_$(date +"%Y%m%d%H%M%S").tar.gz"
    tar --remove-files -czf ${_file_path} ${_work_dir%/}/*.out
    echo "Completed! (${_file_path})"
}

if [ "$0" = "$BASH_SOURCE" ]; then
    if [ -z "$1" ]; then
        echo "$BASH_SOURCE PID [workspace dir]"
        echo "Example:"
        echo "    "$BASH_SOURCE '"`cat /var/run/hadoop/hdfs/hadoop-hdfs-journalnode.pid`"'
        echo "    "$BASH_SOURCE '"`cat /var/run/hadoop/hdfs/hadoop-hdfs-datanode.pid`"'
        echo "    "$BASH_SOURCE '"`cat /var/run/hadoop/hdfs/hadoop-hdfs-namenode.pid`"'
        exit
    fi

    if [ "$USER" != "root" ]; then
        echo "Please run as 'root' user"
        exit 1
    fi
    syschk "$1" "$2" "$3"
fi
