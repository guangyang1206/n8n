import type { ICredentialType, INodeProperties } from 'n8n-workflow';

export class OnfleetApi implements ICredentialType {
	name = 'onfleetApi';

	displayName = 'Onfleet API';

	documentationUrl = 'onfleet';

	properties: INodeProperties[] = [
		{
			displayName: 'API Key',
			name: 'apiKey',
			type: 'string',
			typeOptions: { password: true },
			default: '',
		},
		{
			displayName: 'Signing Secret',
			name: 'signingSecret',
			type: 'string',
			typeOptions: { password: true },
			default: '',
			description:
				'Used to verify webhook authenticity. Find it in your Onfleet dashboard under Configuration → API & Webhooks → Show Secret.',
		},
	];
}
