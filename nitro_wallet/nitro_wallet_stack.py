#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: MIT-0
import aws_cdk
from aws_cdk import (
    Token,
    Stack,
    Fn,
    Duration,
    CfnOutput,
    aws_ec2,
    aws_iam,
    aws_ecr_assets,
    aws_secretsmanager,
    aws_lambda,
    aws_kms
)
from constructs import Construct


class NitroWalletStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        params = kwargs.pop('params')
        super().__init__(scope, construct_id, **kwargs)

        application_type = params["application_type"]

        encrypted_key = aws_secretsmanager.Secret(self, "SecretsManager")

        # key to encrypt stored private keys - key rotation can be enabled in this scenario since that the
        # key id is encoded in the cypher text metadata
        encryption_key = aws_kms.Key(self, "EncryptionKey",
                                     enable_key_rotation=True
                                     )
        encryption_key.apply_removal_policy(aws_cdk.RemovalPolicy.DESTROY)

        signing_server_image = aws_ecr_assets.DockerImageAsset(self, "EthereumSigningServerImage",
                                                               directory="./application/{}/server".format(
                                                                   application_type),
                                                               platform=aws_ecr_assets.Platform.LINUX_AMD64,
                                                               build_args={"REGION_ARG": self.region}
                                                               )

        signing_enclave_image = aws_ecr_assets.DockerImageAsset(self, "EthereumSigningEnclaveImage",
                                                                directory="./application/{}/enclave".format(
                                                                    application_type),
                                                                platform=aws_ecr_assets.Platform.LINUX_AMD64,
                                                                build_args={"REGION_ARG": self.region}
                                                                )

        vpc = aws_ec2.Vpc(self, 'VPC',
                          nat_gateways=1,
                          subnet_configuration=[aws_ec2.SubnetConfiguration(name='public',
                                                                            subnet_type=aws_ec2.SubnetType.PUBLIC),
                                                aws_ec2.SubnetConfiguration(name='private',
                                                                            subnet_type=aws_ec2.SubnetType.PRIVATE_WITH_EGRESS),
                                                ],
                          enable_dns_support=True,
                          enable_dns_hostnames=True)

        aws_ec2.InterfaceVpcEndpoint(
            self, "KMSEndpoint",
            vpc=vpc,
            subnets=aws_ec2.SubnetSelection(subnet_type=aws_ec2.SubnetType.PRIVATE_WITH_EGRESS),
            service=aws_ec2.InterfaceVpcEndpointAwsService.KMS,
            private_dns_enabled=True
        )
        aws_ec2.InterfaceVpcEndpoint(
            self, "SecretsManagerEndpoint",
            vpc=vpc,
            subnets=aws_ec2.SubnetSelection(subnet_type=aws_ec2.SubnetType.PRIVATE_WITH_EGRESS),
            service=aws_ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER,
            private_dns_enabled=True
        )

        aws_ec2.InterfaceVpcEndpoint(
            self, 'SSMEndpoint',
            vpc=vpc,
            subnets=aws_ec2.SubnetSelection(subnet_type=aws_ec2.SubnetType.PRIVATE_WITH_EGRESS),
            service=aws_ec2.InterfaceVpcEndpointAwsService.SSM,
            private_dns_enabled=True
        )

        aws_ec2.InterfaceVpcEndpoint(
            self, 'ECREndpoint',
            vpc=vpc,
            subnets=aws_ec2.SubnetSelection(subnet_type=aws_ec2.SubnetType.PRIVATE_WITH_EGRESS),
            service=aws_ec2.InterfaceVpcEndpointAwsService.ECR,
            private_dns_enabled=True
        )

        nitro_instance_sg = aws_ec2.SecurityGroup(
            self,
            "Nitro",
            vpc=vpc,
            allow_all_outbound=True,
            description="Private SG for NitroWallet EC2 instance")

        # external members (nlb) can run a health check on the EC2 instance 443 port
        nitro_instance_sg.add_ingress_rule(aws_ec2.Peer.ipv4(vpc.vpc_cidr_block),
                                           aws_ec2.Port.tcp(443))

        # all members of the sg can access each others https ports (443)
        nitro_instance_sg.add_ingress_rule(nitro_instance_sg,
                                           aws_ec2.Port.tcp(443))

        # Custom AMI ID provided by your organization
        custom_ami_id = "ami-017e529989798198e"

        # Instance Role and SSM Managed Policy
        role = aws_iam.Role(self, "InstanceSSM",
                            assumed_by=aws_iam.ServicePrincipal("ec2.amazonaws.com")
                            )
        role.add_managed_policy(aws_iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AmazonEC2RoleforSSM"))

        block_device = aws_ec2.BlockDevice(device_name="/dev/xvda",
                                        volume=aws_ec2.BlockDeviceVolume(
                                            ebs_device=aws_ec2.EbsDeviceProps(
                                                volume_size=32,
                                                volume_type=aws_ec2.EbsDeviceVolumeType.GP2,
                                                encrypted=True,
                                                delete_on_termination=True if params.get(
                                                    'deployment') == "dev" else False,
                                            )))

        mappings = {"__DEV_MODE__": params["deployment"],
                    "__SIGNING_SERVER_IMAGE_URI__": signing_server_image.image_uri,
                    "__SIGNING_ENCLAVE_IMAGE_URI__": signing_enclave_image.image_uri,
                    "__REGION__": self.region}

        with open("./user_data/user_data.sh") as f:
            user_data_raw = Fn.sub(f.read(), mappings)

        signing_enclave_image.repository.grant_pull(role)
        signing_server_image.repository.grant_pull(role)
        encrypted_key.grant_read(role)

        machineImage = aws_ec2.GenericLinuxImage({
            'eu-west-1': custom_ami_id})
        
        nitro_instance = aws_ec2.Instance(self, "NitroEC2Instance",
                                        instance_type=aws_ec2.InstanceType("m5a.xlarge"),
                                        machine_image=machineImage,
                                        block_devices=[block_device],
                                        role=role,
                                        security_group=nitro_instance_sg,
                                        vpc=vpc,
                                        vpc_subnets=aws_ec2.SubnetSelection(
                                            subnet_type=aws_ec2.SubnetType.PRIVATE_WITH_EGRESS),
                                        user_data=aws_ec2.UserData.custom(user_data_raw)
                                        )


        invoke_lambda = aws_lambda.Function(self, "NitroInvokeLambda",
                                            code=aws_lambda.Code.from_asset(
                                                path="lambda_/{}/NitroInvoke".format(params["application_type"])),
                                            handler="lambda_function.lambda_handler",
                                            runtime=aws_lambda.Runtime.PYTHON_3_8,
                                            timeout=Duration.minutes(2),
                                            memory_size=256,
                                            environment={"LOG_LEVEL": "DEBUG",
                                                         "NITRO_INSTANCE_PRIVATE_DNS": nitro_instance.instance_private_dns_name,
                                                         "SECRET_ARN": encrypted_key.secret_full_arn,
                                                         "KEY_ARN": encryption_key.key_arn
                                                         },
                                            vpc=vpc,
                                            vpc_subnets=aws_ec2.SubnetSelection(
                                                subnet_type=aws_ec2.SubnetType.PRIVATE_WITH_EGRESS
                                            ),
                                            security_groups=[nitro_instance_sg]
                                            )

        encrypted_key.grant_write(invoke_lambda)
        # if productive case, lambda is just allowed to set the secret key value
        if params.get("deployment") == "dev":
            encrypted_key.grant_read(invoke_lambda)

        CfnOutput(self, "EC2 Instance Role ARN",
                  value=role.role_arn,
                  description="EC2 Instance Role ARN")

        CfnOutput(self, "Lambda Execution Role ARN",
                  value=invoke_lambda.role.role_arn,
                  description="Lambda Execution Role ARN")

        CfnOutput(self, "Instance ID",
                  value=nitro_instance.instance_id,
                  description="EC2 Instance ID")

        CfnOutput(self, "KMS Key ID",
                  value=encryption_key.key_id,
                  description="KMS Key ID")


'''In the modified code:

The load balancer (NLB) and auto scaling group (ASG) related code has been removed.
The nitro_instance has been added using the aws_ec2.Instance class, which represents a single EC2 instance.
The nitro_instance is associated with the security group nitro_instance_sg.
The nitro_instance is referenced when setting the environment variable NITRO_INSTANCE_PRIVATE_DNS for the invoke_lambda function.'''