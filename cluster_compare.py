#!/usr/bin/python

############################################################################
#
# This is a python script to compare configurations of 2 clusters
# by comparing the latest active configs stored in Ambari Database.
#
# This tool can be run from Windows, Mac or Linux machines installed with
# python 2.x and one of the python modules : 'pycurl' or 'requests'
# The machine should have access to both Ambari Server nodes on each clusters
#
# Type 'python cluster-compare.py' and press enter for running instruction
#
# For any questions or suggestions please contact : ayusuf@hortonworks.com
############################################################################

import sys
import json
import cStringIO
import difflib
import random
import time
import getpass
import collections
#import pprint

# Change the default values for the below parameters
# ==================================================
usernameA = 'admin'
usernameB = 'admin'
ambariPortA = '8080'
ambariPortB = '8080'
# Please add the name of the property you would like to mask below array
propertiesToBeMasked = [
'oozie.service.JPAService.jdbc.password',
'javax.jdo.option.ConnectionPassword'
]
# ==================================================

#------Some good colors-----
whitesmoke = '#f5f5f5'
nicered = '#DC9696'
nicegreen = '#8CCBA3'
niceblue = '#137eb8'
bestblue = '#15a7dc'
headergreen = '#4CAF50'
shadowcolor = '#666' # '#888888'

darkgray = '#666'
bluegreen = '#11c1d2'
verylightred = '#f89e90'
lightred = '#d47577'
littlemorered = '#eb5f5d'
anotherred = '#ff8081'
lightgreen = '#8ecd8f'
lightblue = '#90bbff'
verylightblue = '#74c9e2'
textblue = '#1f9dd5'
lightyellow = '#efcc08'
lightorange = '#f8b25d'
verylightorange = '#f8c990'
darkred = '#bd383d'

# ========= Some good Fonts ======================================
georrgia = 'Georgia, Georgia, serif;'
palatino = "'Palatino Linotype', 'Book Antiqua', Palatino, serif;"
lucida = "'Lucida Console', Monaco, monospace"
# ================================================================

hFont = georrgia
pFont = georrgia
aFont = georrgia
thFont = palatino
tdFont = palatino

#blue grey
comb1 = '#5a7ba6'
comb2 = '#9caac7'
# pink green
comb1 = '#ffccca'
comb2 = '#d7e4bb'
# another ok one
comb1 = bluegreen
comb2 = lightorange

#bgDummy =  '#edf2df' # '#dae5bf' # '#70cab9' # '#9cb794' # '#d1c4ab'  #'#a9dca8'
#bgDummy = '#7abd9b'
bgDummy = nicegreen # '#379b7c' # nicegreen
#bgDummy = '#a0c3ab'
bgExist = comb1 # '#94c2f3' # '#ffd2d1' # '#f3a824' # '#dd9a92' #'#2ea4cb'  # '#30a2c8'
bgDifferent = '#30a2c8'
#bgExist = '#f9c0b9'
bgDummy = nicegreen
bgExist = verylightred
bgExist = '#f57a5f'
bgDummy = '#90ce93'
bgExist = '#f67b62'
bgDummy = '#90cc92'
bgExist = '#f89e90'
bgDummy = '#acdab6'
bgExist = '#f9c2ac'
bgDummy = lightgreen # '#c5e5dd'
bgExist = '#f9a562'
bgDummy = '#90cc92' # lightgreen 
selectionblue = '#7dd4f6'

bgHighlight = verylightred
bgHeader = headergreen
bgAlternate = '#f2f2f2'
#bgAlternate = whitesmoke

blankSpace = '&nbsp;'
classTemplate = 'class="%s"'

outputFile = sys.stdout
clusterAHeading = ''
clusterBHeading = ''
bgNone = ''
strMissingConfiguration = '*** Not Configured ***'
diffData = list()

def printLine(line):
	outputFile.write(line + '\n')

def printStyleSheet():
	printLine('<style>')
	printLine('div {')
	printLine('    padding: 0px 20px 20px 20px;')
	#printLine('    border: 1px solid #ddd;')
	printLine('}')
	printLine('h1,h2,h3,h4,h5,h6 {')
	printLine('    font-family: %s;' % hFont)
	printLine('}')
	#printLine('h1 {')
	#printLine('    padding-top: 20px;')
	#printLine('}')
	#printLine('h2 {')
	#printLine('    padding-top: 15px;')
	#printLine('}')
	#printLine('h3 {')
	#printLine('    padding-top: 10px;')
	#printLine('}')
	printLine('a {')
	printLine('    font-family: %s;' % aFont)
	printLine('}')
	printLine('p {')
	printLine('    font-family: %s;' % pFont)
	printLine('}')
	printLine('th {')
	printLine('    font-family: %s;' % thFont)
	printLine('    text-align: left;')
	printLine('    color: %s;' % 'white')
	printLine('    background-color: %s;' % bgHeader)
	printLine('    border-bottom: 1px solid #ddd;')
	printLine('    padding: 5px;')
	printLine('}')
	printLine('th.sub {')
	printLine('    text-align: left;')
	#printLine('    background-color: %s;' % darkgray)
	printLine('}')
	printLine('th.subright {')
	printLine('    text-align: right;')
	#printLine('    background-color: %s;' % darkgray)
	printLine('}')
	printLine('tr {')
	printLine('    border-bottom: 1px solid #ddd;')
	printLine('}')
	printLine('tr.alternate {')
	printLine('    border-bottom: 0px solid #ddd;')
	printLine('}')
	printLine('td {')
	printLine('    font-family: %s;' % tdFont)
	printLine('    padding-left: 5px;')
	printLine('    padding-right: 5px;')
	printLine('    vertical-align: top;')
	printLine('    word-wrap: break-word;')
	#printLine('    border-bottom: 1px solid #ddd;')
	printLine('}')
	printLine('td.highlight {')
	printLine('    background-color: %s;' % bgHighlight)
	printLine('    border-bottom: 1px solid #ddd;')
	printLine('}')
	printLine('td.exists {')
	printLine('    background-color: %s;' % bgExist)
	printLine('    border-bottom: 1px solid #ddd;')
	printLine('}')
	printLine('td.dummy {')
	printLine('    background-color: %s;' % bgDummy)
	printLine('    border-bottom: 1px solid #ddd;')
	printLine('}')
	printLine('td.different {')
	printLine('    background-color: %s;' % bgHighlight)
	printLine('    border-bottom: 1px solid #ddd;')
	printLine('}')
	printLine('td.extproperty {') 
	printLine('    border-bottom: 1px solid #ddd;')
	printLine('}')
	printLine('td.separator {')
	printLine('    background-color: %s;' % bgHeader)
	printLine('    padding: 1px;')
	printLine('}')
	printLine('table {')
	printLine('    table-layout: fixed;')
	printLine('    border-collapse: collapse;')
	printLine('    border: 1px solid #ddd;')
	printLine('    width: 100%;')
	printLine('    -moz-box-shadow: 0 0 15px %s' % shadowcolor)
	printLine('    -webkit-box-shadow: 0 0 15px %s;' % shadowcolor)
	printLine('    box-shadow: 0 0 15px %s' % shadowcolor)
	printLine('}')
	printLine('tr:nth-child(even) {')
	printLine('    background-color: %s' % bgAlternate)
	printLine('}')
	printLine('tr.alternate:nth-child(even) {')
	printLine('    background-color: %s' % whitesmoke)
	printLine('}')
	printLine('</style>')

def runUsingPyCurl(url, username, password):
	c = pycurl.Curl()
	c.setopt(pycurl.URL, url)
	s = cStringIO.StringIO()
	c.setopt(c.WRITEFUNCTION, s.write)
	c.setopt(pycurl.USERPWD, (username + ':' + password))
	c.setopt(pycurl.SSL_VERIFYPEER, 0)   
	c.setopt(pycurl.SSL_VERIFYHOST, 0)
	c.perform()
	response = c.getinfo(pycurl.HTTP_CODE)
	if response != 200:
		errorString = "Error executing the URL: '%s' for the username: '%s'\n" % (url, username)
		sys.stderr.write(errorString)
		sys.exit(2)
	return s.getvalue()

def runUsingRequests(url, username, password):
	r = requests.get(url, auth=(username, password), verify=False)
	if r.status_code != 200:
		errorString = "Error executing the URL: '%s' for the username: '%s'\n" % (url, username)
		sys.stderr.write(errorString)
		sys.exit(2)
	return r.text

def getURLData(url, username, password):
	if module == 'requests':
		return runUsingRequests(url, username, password)
	elif module == 'pycurl':
		return runUsingPyCurl(url, username, password)
		
def printHeader():
	headingAndTitle = 'Cluster comparison - %s and %s' % (clusterAHeading, clusterBHeading)

	printLine('<!DOCTYPE html>')
	printLine('<html>')

	printLine('<head>')
	printLine('<title>%s</title>' % headingAndTitle)
	printStyleSheet()
	printLine('</head>')

	printLine('<body>')
	printLine('<div class=pagePadding>')
	printLine('<h1></br>%s</h1>' % headingAndTitle)
	printLine('<p>Generated on : %s</p>' % time.strftime("%a, %d %b %Y %I:%M %p"))

def dumpExtendedDiff(data):
	printLine('<table>')
	printLine('<tr>')
	printLine('<th>%s</th>' % clusterAHeading)
	printLine('<th>%s</th>' % clusterBHeading)
	printLine('</tr>')

	for l, hl, r, hr in data:
		if not l:
			l = blankSpace
		if not r:
			r = blankSpace
		printLine('<tr>')
		printLine('<td class="%s">%s</td>' % (hl, l))
		printLine('<td class="%s">%s</td>' % (hr, r))
		printLine('</tr>')
	printLine('</table>')

def storeDiffDataAndGetID(serviceAndType, left, right):
	leftList = str(left).splitlines()
	rightList = str(right).splitlines()

	d = difflib.Differ()
	diff = d.compare(leftList, rightList)

	good = list()
	for line in diff:
		line = line.strip()
		if line and line[0] == '-':
			good.append((line[1:], 'exists', '', 'dummy'))
		elif line and line[0] == '+':
			good.append(('', 'dummy', line[1:], 'exists'))
		elif line and line[0] == '?':
			pass
		else:
			good.append((line, '', line, ''))

	id = serviceAndType.replace(':','_').replace(' ','') + str(random.randrange(100, 999, 3))
	heading = '<h3 id=%s></br>Appendix - %s : %s extended diff</h3>' % (id, len(diffData)+1, serviceAndType)

	diffData.append((heading, good))
	return id

def calcDiffData(left, right):
	leftList = str(left).splitlines()
	rightList = str(right).splitlines()

	d = difflib.Differ()
	diffResult = list(d.compare(leftList, rightList))
	
	diffList = list()
	prevOperator = ''
	for line in diffResult:
		operator = line[0]
		line = line[1:].strip()

		if operator == '-':
			diffList.append((line, 'exists', '', 'dummy'))
		elif operator == '+':
			if prevOperator == '?':
				l,lh,r,rh = diffList[-1]
				lh,r,rh = 'different',line,'different'
				diffList[-1] = (l,lh,r,rh)
			else:
				diffList.append(('', 'dummy', line, 'exists'))
		elif operator == '?':
			pass
		else:
			diffList.append((line, '', line, ''))
		prevOperator = operator
	return diffList


def compareAndDumpHTML(service, type, dataA, dataB):
	mergedProps = sorted(set(dataA.keys() + dataB.keys()))
	if mergedProps is None:
		return

	printLine('<table>')
	printLine('<tr>')
	printLine('<th %s>%s : %s</th>' % ('width="24%"', service, type))
	printLine('<th %s>%s</th>' % ('width="38%"', clusterAHeading))
	printLine('<th %s>%s</th>' % ('width="38%"', clusterBHeading))
	printLine('</tr>')

	atleastOneProp = False
	extendedDiffList = list()
	for prop in mergedProps:
		valueA = valueB = strMissingConfiguration
		if prop in dataA:
			valueA = dataA[prop].strip()
		if prop in dataB:
			valueB = dataB[prop].strip()

		if prop == 'content':
			propName = type + ' template'
			heading = service + ' : ' + propName
		else:
			propName = prop
			heading = service + ' : ' + type + ' : ' + propName

		link = ''
		classTag = ''
		if valueA != valueB:
			classTag = classTemplate % 'highlight'

		if '\n' in valueA or '\n' in valueB:
			#id = storeDiffDataAndGetID(heading, valueA, valueB)
			#link = 'href="#%s"' % id
			#propName = propName + '</br>Click here for extended comparison'
			extendedDiffList.append((propName, calcDiffData(valueA, valueB)))
			#printExtendedComparison(propName, calcDiffData(valueA, valueB))
		else:
			atleastOneProp = True
			printLine('<tr>')
			printLine('<td %s %s><a %s>%s</a></td>' % (classTag, 'width="500"', link, propName.strip()))
			printLine('<td %s>%s</td>' % (classTag, valueA.strip().replace('\n','</br>')))
			printLine('<td %s>%s</td>' % (classTag, valueB.strip().replace('\n','</br>')))
			printLine('</tr>')

	for prop, data in extendedDiffList:
		if atleastOneProp:
			printLine('<tr>')
			printLine('<td %s %s></th>' % ('colspan="3"', classTemplate % 'separator'))
			printLine('</tr>')
		printExtendedComparison(prop, data)
	printLine('</table>')

def printExtendedComparison(propName, diffList):
	#printLine('<tr>')
	#printLine('<td %s %s></th>' % ('colspan="3"', classTemplate % 'separator'))
	#printLine('</tr>')

	noRows = len(diffList)

	firstRow = True
	for l, hl, r, hr in diffList:
		if not l:
			l = blankSpace
		if not r:
			r = blankSpace
		printLine('<tr class="%s">' % 'alternate')
		if firstRow:
			printLine('<td class="%s" %s>%s</td>' % ('extproperty', ('rowspan="%s"' % noRows), propName))
			firstRow = False
		printLine('<td class="%s">%s</td>' % (hl, l))
		printLine('<td class="%s">%s</td>' % (hr, r))
		printLine('</tr>')

	#printLine('<tr>')
	#printLine('<td %s %s></th>' % ('colspan="3"', classTemplate % 'separator'))
	#printLine('</tr>')

def getClusterNameAsJSON(ambariServer, ambariPort, username, password):
	url = 'http://%s:%s/api/v1/clusters/' % (ambariServer, ambariPort)
	return json.loads(getURLData(url, username, password))['items'][0]['Clusters']['cluster_name']

def getMaskedPropertyValues(listProperties, cluster):
	for prop in listProperties:
		if str(prop).strip() in propertiesToBeMasked:
			listProperties[prop] = '[*** Masked ***]'
	return listProperties


def getAllConfigs(ambariServer, ambariPort, username, password, cluster, services):
	base_url = 'http://%s:%s/api/v1/clusters/%s' % (ambariServer, ambariPort, cluster)
	config_versions_url = base_url + '/configurations/service_config_versions?service_name.in(%s)&is_current=true'

	dictServices = {}
	for service in services:
		typeItems = json.loads(getURLData(config_versions_url % (service), username, password))['items']
		dictGroups = {}
		for y in typeItems:
			listTypes = {}
			if y['group_id'] == -1:
				group_name = 'Default'
			else:
				group_name = y['group_name']
			for z in y['configurations']:
				listTypes[z['type']] = getMaskedPropertyValues(z['properties'], cluster)
			dictGroups[group_name] = listTypes
		dictServices[service] = dictGroups

	return dictServices

def printFooter():
	printLine('</div>')
	printLine('<body>')
	printLine('<html>')

def getServiceVerMap(ambariServer, ambariPort, username, password, cluster):
	base_url = 'http://%s:%s/api/v1' % (ambariServer, ambariPort)
	services = []
	items = json.loads(getURLData('%s/clusters/%s/services' % (base_url, cluster), username, password))['items']
	for item in items:
		services.append(str(item['ServiceInfo']['service_name']))

	service_details = json.loads(getURLData('%s/clusters/%s' % (base_url, cluster), username, password))['Clusters']['desired_service_config_versions']

	serviceVersionMap = {}
	for service in services:
		try:
			stack, stack_version = str(service_details[service][0]['stack_id']).split('-',2)
			service_url = '%s/stacks/%s/versions/%s/services/%s' % (base_url, stack, stack_version, service)
			version = json.loads(getURLData(service_url, username, password))['StackServices']['service_version']
			stackAndVersion = stack + '-' + stack_version + ' (V ' + version + ')'
		except:
			serviceVersionMap[service] = 'Unknown'
		else:
			serviceVersionMap[service] = stackAndVersion
	return serviceVersionMap

def getGroupsIDandLabel(configData, clusterHeading, service):
	cluster = clusterHeading.replace(' ','').replace('(','').replace(')','')
	grps = {}
	if service in configData:
		grps = sorted(configData[service].keys())
	link = 'Default'
	for grp in grps:
		if grp != 'Default':
			#link += ',  <a href=#%s>%s</a>' % (cluster + '_' + service + '_' + str(grp).replace(' ','_'), str(grp))
			link += ',  <a href=#%s>%s</a>' % ('CustomConfigGroups', str(grp))
	return link

def printServiceComparisonTableAsHTML(clusterA, serviceVerMapA, configDataA, clusterB, serviceVerMapB, configDataB):
	printLine('<h1></br>Installed Services</h1>')
	printLine('<table>')
	printLine('<tr>')
	printLine('<th %s %s>%s</th>' % ('width="14%"', 'rowspan="2"', 'Service'))
	printLine('<th %s %s>%s</th>' % ('width="43%"', 'colspan="3"', clusterAHeading))
	printLine('<th %s %s>%s</th>' % ('width="43%"', 'colspan="3"', clusterBHeading))
	printLine('</tr>')
	printLine('<tr>')
	printLine('<th %s>%s</th>' % ('width="10%"', 'Installed?'))
	printLine('<th %s>%s</th>' % ('width="20%"', 'Config Groups'))
	printLine('<th %s>%s</th>' % ('width="13%"', 'Stack (Version)'))
	printLine('<th %s>%s</th>' % ('width="10%"', 'Installed?'))
	printLine('<th %s>%s</th>' % ('width="20%"', 'Config Groups'))
	printLine('<th %s>%s</th>' % ('width="13%"', 'Stack (Version)'))
	printLine('</tr>')

	serviceMergedList = sorted(set(serviceVerMapA.keys() + serviceVerMapB.keys()))
	for service in serviceMergedList:
		classTag = ''
		colA1 = colB1 = grpLinkA = grpLinkB = '-'
		colA3 = colB3 = 'Default'
		colA2 = colB2 = '-'
		if service in serviceVerMapA.keys():
			colA1 = 'Yes'
			colA2 = serviceVerMapA[service]
			grpLinkA = getGroupsIDandLabel(configDataA, clusterAHeading, service)
		if service in serviceVerMapB.keys():
			colB1 = 'Yes'
			colB2 = serviceVerMapB[service]
			grpLinkB = getGroupsIDandLabel(configDataB, clusterBHeading, service)
		if colA1 != colB1:
			classTag = classTemplate % 'highlight'

		printLine('<tr>')
		printLine('<td %s><a %s>%s</a></td>' % (classTag, 'href="#%s"' % service, service))

		printLine('<td %s>%s</td>' % (classTag, colA1))
		printLine('<td %s>%s</td>' % (classTag, grpLinkA))
		printLine('<td %s>%s</td>' % (classTag, colA2))

		printLine('<td %s>%s</td>' % (classTag, colB1))
		printLine('<td %s>%s</td>' % (classTag, grpLinkB))
		printLine('<td %s>%s</td>' % (classTag, colB2))
		printLine('</tr>')
	printLine('</table>')
	return serviceMergedList

def printConfigTypeComparisonTablesAsHTML(serviceMergedList, configDataA, configDataB):
	service_count = 1
	for service in serviceMergedList:
		printLine('<h2 %s></br>%d. %s Service Configurations</h2>' % ('id=%s' % service, service_count, service))

		if service in configDataA.keys():
			listTypesA = configDataA[service]['Default']
		else:
			listTypesA = {}
		if service in configDataB.keys():
			listTypesB = configDataB[service]['Default']
		else:
			listTypesB = {}

		configTypeMergedList = sorted(set(listTypesA.keys() + listTypesB.keys()))
		type_count = 1
		for type in configTypeMergedList:
			printLine('<h3></br>%d.%d. %s : %s</h3>' % (service_count, type_count, service, type))

			if type in listTypesA.keys():
				propsA = listTypesA[type]
			else:
				propsA = {}
			if type in listTypesB.keys():
				propsB = listTypesB[type]
			else:
				propsB = {}

			compareAndDumpHTML(service, type, propsA, propsB)
			type_count += 1

		service_count += 1

def splitConfigGroups(configData):
	defaultCGData = {}
	otherCGsData = {}
	for service in configData:
		defaultGP = {}
		otherGP = {}
		for group in configData[service]:
			if group == 'Default':
				defaultGP[group] = configData[service][group]
			else:
				otherGP[group] = configData[service][group]
		defaultCGData[service] = defaultGP
		if len(otherGP) > 0:
			otherCGsData[service] = otherGP
	return defaultCGData, otherCGsData

def printOtherConfigGroupsTablesAsHTML(newList):
	printLine('<h2 %s></br>Custom Config Groups</h2>' % ('id=%s' % 'CustomConfigGroups'))

	service_count = 1
	for item in newList:
		printLine('<h3></br>%d. %s</h3>' % (service_count, item))
		firstRow = True
		printLine('<table>')
        	printLine('<tr>')
        	printLine('<th %s>%s</th>' % ('width="25%"', 'Cluster Name'))
        	printLine('<th %s>%s</th>' % ('width="25%"', 'Config Group'))
        	printLine('<th %s>%s</th>' % ('width="25%"', 'Property (Key)'))
        	printLine('<th %s>%s</th>' % ('width="25%"', 'Value'))
        	printLine('</tr>')
		for prop, dummyC, dummyCG, cluster, configgroup, value in newList[item]:
			tag = ''
			if dummyCG == 0:
				tag = '<b>'
				if not firstRow:
					printLine('<tr>')
					printLine('<td %s %s></th>' % ('colspan="4"', classTemplate % 'separator'))
					printLine('</tr>')
					printLine('<tr>')
					printLine('</tr>')
                	printLine('<tr>')
                	printLine('<td>%s%s%s</td>' % (tag, cluster, tag))
                	printLine('<td>%s%s%s</td>' % (tag, configgroup, tag))
                	printLine('<td>%s%s%s</td>' % (tag, prop, tag))
                	printLine('<td>%s%s%s</td>' % (tag, value, tag))
			printLine('</tr>')
			firstRow = False
        	printLine('</table>')
		service_count += 1

def getSortedConfigGroupsList(otherCGsDataA, otherCGsDataB, defaultCGDataA, defaultCGDataB):
	serviceTypeList = {}

	for service in otherCGsDataA:
		for group in otherCGsDataA[service]:
			for type in otherCGsDataA[service][group]:
				key = service + ' : ' + type
				if key not in serviceTypeList:
					serviceTypeList[key] = set()
				for prop in otherCGsDataA[service][group][type]:
					serviceTypeList[key].add((prop, 'A', 1, clusterAHeading, group, otherCGsDataA[service][group][type][prop]))
					if prop in defaultCGDataA[service]['Default'][type]:
						serviceTypeList[key].add((prop, 'A', 0, clusterAHeading, 'Default', defaultCGDataA[service]['Default'][type][prop]))
					else:
						serviceTypeList[key].add((prop, 'A', 0, clusterAHeading, 'Default', strMissingConfiguration))

	for service in otherCGsDataB:
		for group in otherCGsDataB[service]:
			for type in otherCGsDataB[service][group]:
				key = service + ' : ' + type
				if key not in serviceTypeList:
					serviceTypeList[key] = set()
				for prop in otherCGsDataB[service][group][type]:
					serviceTypeList[key].add((prop, 'B', 1, clusterBHeading, group, otherCGsDataB[service][group][type][prop]))
					if prop in defaultCGDataB[service]['Default'][type]:
						serviceTypeList[key].add((prop, 'B', 0, clusterBHeading, 'Default', defaultCGDataB[service]['Default'][type][prop]))
					else:
						serviceTypeList[key].add((prop, 'B', 0, clusterBHeading, 'Default', strMissingConfiguration))

	sortedServiceTypeList = {}
	for serviceType in serviceTypeList:
		sortedServiceTypeList[serviceType] = sorted(serviceTypeList[serviceType])

	return collections.OrderedDict(sorted(sortedServiceTypeList.items()))


############################################
# Program start
############################################
module = 'none'
try:
	import requests
except:
	try:
		import pycurl
	except:
		errorString = "Error:\n"
		errorString += "  Could not import 'requests' or 'pycurl' module. One of them is required.\n"
		errorString += "  Try running from one of the Ambari Server hosts; it should have 'pycurl' module installed.\n"
		errorString += "  You can test by running \"python -c 'import pycurl'\" or \"python -c 'import requests'\". This should NOT throw any error.\n"
		errorString += "  You may use 'pip install' or 'easy_install' to install 'requests' or 'pycurl' module.\n"
		sys.stderr.write(errorString)
		sys.exit(2)
	else:
		module = 'pycurl'
else:
	module = 'requests'


ambariServerA = ''
ambariServerB = ''
clusterA = ''
clusterB = ''

if len(sys.argv) < 3:
	sys.stderr.write('python ' + sys.argv[0] + ' <ambariServer1> <ambariServer2> [<username1>] [<username2>] [<cluster1>] [<cluster2>] [<port1>] [<port2>]\n')
	sys.stderr.write('Options:\n')
	sys.stderr.write('    ambariServer1 : Required. IP/Hostname of first Ambari Server\n')
	sys.stderr.write('    ambariServer2 : Required. IP/Hostname of second Ambari Server\n')
	sys.stderr.write('    username1 : Optional. Username for first Ambari. Default: "%s". Will be promted for the password\n' % usernameA)
	sys.stderr.write('    username2 : Optional. Username for second Ambari. Default: "%s". Will be promted for the password\n' % usernameB)
	sys.stderr.write('    cluster1 : Optional. Name of the first cluster. Default: First available cluster name of "ambariServer1"\n')
	sys.stderr.write('    cluster2 : Optional. Name of the second cluster. Default: First available cluster name of "ambariServer2"\n')
	sys.stderr.write('    port1 : Optional. Port number for first Ambari Server. Default: "%s"\n' % ambariPortA)
	sys.stderr.write('    port2 : Optional. Port number for second Ambari Server. Default: "%s"\n' % ambariPortB)
	sys.stderr.write('Note:\n')
	sys.stderr.write('    All the parameters should be supplied in the same order.\n')
	sys.stderr.write('    If any optional parameters are needed, then all the previous optional parameters should be provided as well\n')
	sys.stderr.write('    If the script takes long time and timesout, then try using the IP Address of Ambari Server instead of hostname.\n')
	sys.exit(2)

for index in range(len(sys.argv)):
	if index == 1:
		ambariServerA = sys.argv[index]
	if index == 2:
		ambariServerB = sys.argv[index]
	if index == 3:
		usernameA = sys.argv[index]
	if index == 4:
		usernameB = sys.argv[index]
	if index == 5:
		clusterA = sys.argv[index]
	if index == 6:
		clusterB = sys.argv[index]
	if index == 7:
		ambariPortA = sys.argv[index]
	if index == 8:
		ambariPortB = sys.argv[index]

passwordA = getpass.getpass('Ambari password for %s [%s]: ' % (ambariServerA, usernameA))
if not passwordA:
	passwordA = 'admin'

passwordB = getpass.getpass('Ambari password for %s [%s]: ' % (ambariServerB, usernameB))
if not passwordB:
	passwordB = 'admin'

# Wraping around str() to convert unicode to str - for using in URL
if not clusterA:
	clusterA = str(getClusterNameAsJSON(ambariServerA, ambariPortA, usernameA, passwordA))
if not clusterB:
	clusterB = str(getClusterNameAsJSON(ambariServerB, ambariPortB, usernameB, passwordB))

clusterAHeading = clusterA + ' (Ambari Server: ' + ambariServerA + ')'
clusterBHeading = clusterB + ' (Ambari Server: ' + ambariServerB + ')'

outFilename = '%s-%s.html' % (clusterA, clusterB)
outputFile = open(outFilename, 'w')

printHeader()

serviceVerMapA = getServiceVerMap(ambariServerA, ambariPortA, usernameA, passwordA, clusterA)
serviceVerMapB = getServiceVerMap(ambariServerB, ambariPortB, usernameB, passwordB, clusterB)

configDataA = getAllConfigs(ambariServerA, ambariPortA, usernameA, passwordA, clusterA, serviceVerMapA.keys())
configDataB = getAllConfigs(ambariServerB, ambariPortB, usernameB, passwordB, clusterB, serviceVerMapB.keys())

serviceMergedList = printServiceComparisonTableAsHTML(clusterA, serviceVerMapA, configDataA, clusterB, serviceVerMapB, configDataB)

defaultCGDataA, otherCGsDataA = splitConfigGroups(configDataA)
defaultCGDataB, otherCGsDataB = splitConfigGroups(configDataB)

printLine('<h1></br>Service: Config Type - Comparison</h1>')

printLine("<p>Note:</br>The comparison is done only for the 'Default' Config Group.")
if len(otherCGsDataA) > 0 or len(otherCGsDataB) > 0:
	printLine('</br><a href=#%s>Click here to jump to Custom Config Groups listing section</a>' % 'CustomConfigGroups')
printLine('</p>')

printConfigTypeComparisonTablesAsHTML(serviceMergedList, defaultCGDataA, configDataB)

sortedList = getSortedConfigGroupsList(otherCGsDataA, otherCGsDataB, defaultCGDataA, defaultCGDataB)

printOtherConfigGroupsTablesAsHTML(sortedList)

printFooter()

sys.stdout.write("\nThe comparison output has been written to the file '%s'\n\n" % outFilename)

