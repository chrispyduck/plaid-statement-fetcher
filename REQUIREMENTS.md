# Bank Statement Fetcher

## Purpose

To fetch montly and quarterly PDF statements from my banks using the Plaid API

## Requirements

This consists of two main components:
1. A configuration utility to "link" bank accounts to plaid
2. A simple "downloader" that fetches statements from all accounts

### Configuration Utility

* Kick off web-based plaid bank account "linking" process
* Store any required information in a local configuration file

### Downloader

* For each "linked" account, get a list of available bank statements
* Compare this list to a stored list of previously fetched statements
* Download any available statements that have not been previously downloaded
* For all successfuly downloaded statement, update the stored list of previously fetched statements

### Common Requirements

* Configuration is stored in one JSON file
* List of downloaded statements is stored in another JSON file
* primarily intended to be deployed on a Kubernetes cluster: includes  Kustomization with Deloyment, Service, Ingres, ConfigMap, and Secrets 
* Also usable as a local application (using localhost or an /etc/hosts file entry depending on Plaid/bank requirements) for testing and develoment