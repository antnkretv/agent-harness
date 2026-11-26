# AWS deployment

Use this reference when the user asks to deploy an Aspire app to AWS or to generate AWS CDK/CloudFormation deployment artifacts.

AWS deployment is currently a preview path driven by the AWS Aspire integrations. Treat the AWS repository as the source of truth, verify the installed package/API shape before editing, and do not invent TypeScript deployment APIs. AWS deployment has no TypeScript AppHost support yet.

## Source of truth and docs lookup

Use the AWS Aspire integrations repository for AWS-specific deployment guidance:

https://github.com/aws/integrations-on-dotnet-aspire-for-aws

Use these repository docs when available:

- `README.md` for the deployment overview and prerequisites.
- `src/Aspire.Hosting.AWS/README.md` for package-level setup.
- `docs/deployment-design.md` for publish target overrides, CDK constructs, and advanced customization.

Do not use `aspire docs search` for AWS deployment guidance. The AWS deployment docs are not in the Aspire docs index; use the AWS repository and deployment design document instead.

## Target setup

Add the AWS integration with:

```bash
aspire add aws
```

Configure the C# AppHost only with APIs supported by both the installed integration
and the project's [diagnostic gates](../../../../docs/platform/architecture/gates.md).
The repository permits no manual diagnostic suppression. The AppHost opts in only to
the experimental IDs its project's .NET rules accept. Extending that list with
`ASPIREAWSPUBLISHERS001` requires the project owner's decision.

If the installed AWS integration requires that preview diagnostic, keep the AppHost
on stable resources and build the application images with their Dockerfiles. Model
AWS infrastructure in a separate CDK project using supported packages, and inspect
its synthesized CloudFormation artifacts with the native CDK CLI. That separate
project owns its resource mapping, connectivity and deployment; it does not inherit
Aspire's AWS mapping or publish pipeline. The Aspire AWS steps below apply only
when the installed integration builds without unsupported diagnostics.

## Code changes to make

Make these changes in the C# AppHost when the AWS integration docs still show the C#-only preview API:

1. Run `aspire add aws` if the AppHost does not already reference `Aspire.Hosting.AWS`.
2. Verify that the installed AWS CDK environment API is stable before adding it near the top of the AppHost. The upstream `CDKDefaultsProviderFactory.Preview_V1` example requires an unsupported preview contract; use the separate CDK project described above while that remains the installed API.
3. Keep existing Aspire resources in the AppHost. Default AWS deployment mapping handles common resources without per-resource publish calls.
4. Keep `WithReference(...)` relationships between resources. The AWS deployment system uses references for environment variables, VPC attachment, and security group connectivity where supported.
5. Add AWS-specific resources only when the app actually needs them and the AWS docs show the current shape, such as `AddAWSLambdaFunction` for Lambda projects/handlers.
6. Add publish override methods only when the user asks for a specific AWS shape or the default mapping is wrong. For example, use an ECS Fargate/ALB publish target only after verifying the current method name in the AWS deployment design document.
7. Do not invent a TypeScript AWS deployment environment. If the AppHost is TypeScript, stop and report that the current AWS deployment integration is C# AppHost-focused.

Do not add AWS CDK constructs directly as the first approach. Start with Aspire resources and references, then use AWS construct callbacks or custom CDK stacks only for explicit infrastructure customization.

## Prerequisites

Check:

- AWS credentials are available for the target account.
- The AWS region is known and matches the user's intended deployment target.
- Node.js 22.x is installed when the AWS CDK tooling requires it.
- AWS CDK is installed and available.
- The target account and region are bootstrapped for CDK before first deployment:

  ```bash
  cdk bootstrap aws://<account-id>/<region>
  ```

- Docker is available when compute resource images need to be built.
- Required AppHost parameters are configured or can be prompted.
- If AWS CDK dependencies produce package signing diagnostics, choose supported signed dependencies or isolate the native CDK tooling outside the AppHost dependency graph. Preserve the build and package integrity checks; do not add project warning exceptions.

Use AWS CLI to verify identity and region before deploying:

```bash
aws sts get-caller-identity
aws configure list
aws configure get region
cdk --version
```

Do not print access keys, secret keys, session tokens, or resolved secret parameter values.

## Preview and publish

Use Aspire publish to generate deployment artifacts before applying them when the user wants review or validation:

```bash
aspire publish --list-steps
aspire publish -o aws-artifacts
```

For AWS CDK deployment, the AWS integration transforms Aspire resources into CDK constructs and synthesizes CloudFormation templates under `cdk.out` in the output location. Inspect the generated CDK output before deploying when the user asked for a preview:

```bash
find aws-artifacts -maxdepth 3 -type f | sort
cdk ls --app aws-artifacts/cdk.out
cdk diff --app aws-artifacts/cdk.out
```

The exact output path can vary with the installed integration; inspect the publish output and generated files instead of assuming a fixed directory layout.

## Deploy

Deploy with:

```bash
aspire deploy
```

The AWS integration runs the publish step and then uses AWS CDK deployment against the configured AWS account and region.

Use `aspire deploy --list-steps` before applying changes when the user asked for validation or when the AppHost has custom AWS publish targets.

## Destroy

Use Aspire to run the AWS target's destroy pipeline:

```bash
aspire destroy --environment <name>
```

For AWS, `aspire destroy` delegates to the AWS deployment target for the selected AppHost/environment. Confirm the AWS account, region, CDK stack name, AppHost, and environment before running destroy. Use `--yes` only after destructive intent is explicit. Use AWS CLI or CDK destroy commands only to investigate failed teardown or clean up resources that the Aspire AWS target did not manage.

## Resource mapping and customization

Current AWS guidance says resources are mapped to AWS services by default and can be overridden with publish extension methods. Keep these rules in mind:

- Web projects can map to ECS Fargate-style targets by default.
- Lambda project resources can map to AWS Lambda.
- Redis can map to ElastiCache.
- `WithReference()` drives connectivity such as environment variables, VPC attachment, and security groups.
- Custom CDK stacks and construct callbacks are advanced customization points; use them only when the user asks for infrastructure customization and verify the exact API in the AWS design document.

Do not assume every Aspire resource has an AWS publish target. If a resource has no supported mapping, call that out before deployment rather than implying it will be provisioned.

## Troubleshooting live AWS state

Use AWS CLI to inspect live resources that Aspire deployed through CDK/CloudFormation:

```bash
aws cloudformation list-stacks --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE UPDATE_ROLLBACK_COMPLETE
aws cloudformation describe-stacks --stack-name "<stack-name>"
aws cloudformation describe-stack-events --stack-name "<stack-name>" --max-items 20
aws cloudformation list-stack-resources --stack-name "<stack-name>"
```

Use service-specific commands based on the resources in the generated CDK output:

```bash
# ECS/Fargate
aws ecs list-clusters
aws ecs list-services --cluster "<cluster-name>"
aws ecs describe-services --cluster "<cluster-name>" --services "<service-name>"

# Lambda
aws lambda list-functions
aws lambda get-function --function-name "<function-name>"

# ElastiCache
aws elasticache describe-cache-clusters --show-cache-node-info
```

When a deploy fails, inspect CloudFormation stack events first, then the specific service that failed. Match live stack/resource names back to the AWS CDK environment name, Aspire deployment output, and generated `cdk.out` templates.

## Common checks

- Confirm the generated AWS stack name and region before deployment.
- Confirm credentials match the intended account.
- Confirm CDK bootstrap has run for the account and region.
- Treat preview deployment APIs as subject to change; re-check the AWS repository before making assumptions.
- When customization is needed, use the AWS repository's deployment design document and verify the exact API for the AppHost language before editing.
- Confirm any AppHost parameters that become CloudFormation/CDK inputs, especially names containing punctuation, by inspecting the synthesized output.
- If an ECS service is unhealthy, inspect ECS service events, task status, logs, image pull permissions, and security groups.
- If CloudFormation rolls back, read the first failed stack event rather than only the final rollback event.
