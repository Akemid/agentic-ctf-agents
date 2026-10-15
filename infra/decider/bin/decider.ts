#!/usr/bin/env node
import { App } from 'aws-cdk-lib';
import { buildStacks } from '../lib/app';

// Two independent stacks in one app:
//   cdk deploy CtfPersistentStack -c budgetEmail=<address>      (once)
//   cdk deploy DeciderStack -c imageTag=<tag>                   (weekly)
buildStacks(new App());
