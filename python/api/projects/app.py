# Core imports
import json, uuid, logging, datetime, os, decimal

# 3rd party imports
import boto3
from boto3.dynamodb.conditions import Key, Attr

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Dynamo controlled attributes
immutable_attributes = ('item_id', 'project_id', 'sort_key', 'object_type', 'object_id', 'updated_on', 'created_on')
# Public attributes
public_attributes = ('item_id', 'project_id', 'object_type', 'object_id', 'updated_on', 'created_on', 'active', 'description', 'source_code_url', 'website', 'tags', 'teams')
public_team_attributes = ('item_id', 'team_id', 'object_type', 'object_id', 'updated_on', 'created_on','team_name')
public_user_attributes = ('user_id','object_type','email','email_verified','created_on','updated_on','item_id','object_id')

# Parse env variables
table_name = os.environ['DYNAMO_TABLE']
# Instantiate boto3 clients and resources
dynamo = boto3.resource('dynamodb')
s3 = boto3.client('s3')
dp_core_table = dynamo.Table(table_name)
    

def lambda_handler(event, context):
    '''
    Projects lambda function

    {
        active: true
        created_on: "2020-08-29T23:59:12.973384"
        description: "This is me"
        item_id: ""
        name: ""
        object_id: "project:5af3d586-9897-45db-bd67-c232112c6de6"
        object_type: "project"
        project_id: "5af3d586-9897-45db-bd67-c232112c6de6"
        published: false
        sort_key: "project:5af3d586-9897-45db-bd67-c232112c6de6"
        source_code_url: "https://github.com/matmertz25/"
        source_control: [{…}]
        tags: ["test", "another tag", "a third tag", "new tag"]
        teams: [{…}, {…}]
        updated_on: "2020-08-30T00:14:14.848210"
        website: "https://"
    }
    '''
    # Log event
    logger.info(event)
    # Get organization from jwt claims
    jwt_claims = event['requestContext']['authorizer']['claims'] if event['requestContext']['authorizer']['claims'] else {}
    groups = jwt_claims.get('cognito:groups').split(',')
    organizations = [group.replace('organization:', '') for group in groups if "organization:" in group]
    # Get user_id from jwt claims
    user_id = jwt_claims.get('sub')
    organization_role = jwt_claims.get('role', None)
    logger.info(f'userId: {user_id}')
    
    # Get http method and query params and strings
    http_method = event['httpMethod']
    event_id = event['requestContext']['requestId']
    source_ip = event['requestContext']['identity']['sourceIp']
    query_string_parameters = event["queryStringParameters"] if event["queryStringParameters"] else {}
    path_parameters = event["pathParameters"] if event["pathParameters"] else {}

    timestamp = datetime.datetime.utcnow().isoformat()

    organization_id = path_parameters['organizationId']
    if organization_id not in organizations:
        return response(None, {"message": "not authorized"})

    if organization_role not in ('manager', 'admin', 'owner') and (http_method == 'POST' or http_method == 'DELETE'):
        return response(None, {"message": "Not authorized"})
    elif organization_role == 'member' and http_method == 'PUT':
        return response(None, {"message": "Not authorized"})

    if http_method == 'POST':
        body = json.loads(event['body'])
        timestamp = datetime.datetime.utcnow().isoformat()
        item_id = str(uuid.uuid4())
        body['item_id'] = organization_id
        body['project_id'] = item_id
        body['object_type'] = 'project'
        body['object_id'] = f'project:{item_id}'
        body['sort_key'] = f'project:{item_id}'
        body['active'] = True
        body['created_on'] = timestamp

        logger.info(body)
        teams = body.pop('teams', [])

        organization_detail = dp_core_table.get_item(
            Key={"item_id": organization_id, "sort_key": 'organization'},
            ProjectionExpression='public_projects'
        )['Item']
        if body.get('public', False) and organization_detail['public_projects'] is not True: 
            return response(None, {"message": "Public projects are disabled for this organization"})

        dp_core_table.put_item(Item=body)

        with dp_core_table.batch_writer() as batch:
            for team_id in teams:
                batch.put_item(Item={
                    "item_id": organization_id,
                    "object_type": 'relationship',
                    "sort_key": f'project:{item_id}:team:{team_id}',
                    "object_id": f'team:{team_id}:project:{item_id}',
                    "project_id": item_id,
                    "team_id": team_id,
                })
        
        event_item(
            body=body, 
            event_id=event_id, 
            description=f'Created project {body["name"]}', 
            actor=user_id, 
            event_type='create', 
            ip_address=source_ip,
        )

        return response({"message": "created"}, None)
    elif http_method == 'PUT':
        body = json.loads(event['body'])
        project_id = path_parameters['projectId']
        project_detail = dp_core_table.get_item(
            Key={"item_id": organization_id, "sort_key": f'project:{project_id}'},
        )['Item']

        organization_detail = dp_core_table.get_item(
            Key={"item_id": organization_id, "sort_key": 'organization'},
            ProjectionExpression='public_projects'
        )['Item']

        if organization_role in ('developer', 'manager'):
            project_teams = dp_core_table.query(
                KeyConditionExpression=Key('item_id').eq(organization_id) & Key('sort_key').begins_with(f'project:{project_id}:team:'),
                ProjectionExpression='team_id'
            )['Items']

            teams = []
            for team_id in project_teams:
                member_detail = dp_core_table.get_item(
                    Key={"item_id": organization_id, "sort_key": f'team:{team_id}:member:{user_id}'},
                ).get('Item')
                teams.append(member_detail)
            if len(teams) == 0:
                return response(None, {"message": "Not authorized"})

        if project_detail['active'] is not True and body.get('active', True) is not True: 
            return response(None, {"message": "Project is not active"})

        if body.get('public', False) and organization_detail['public_projects'] is not True: 
            return response(None, {"message": "Public projects are disabled for this organization"})

        project_teams = dp_core_table.query(
            KeyConditionExpression=Key('item_id').eq(organization_id) & Key('sort_key').begins_with(f'project:{project_id}:team:'),
            ProjectionExpression='team_id'
        )['Items']
        project_team_ids = [team['team_id'] for team in project_teams]
        teams = body.pop('teams', [])
        add_teams = list(set(teams) - set(project_team_ids))
        remove_teams = list(set(project_team_ids) - set(teams))

        body = {k: v for k, v in body.items() if k not in immutable_attributes}
        project_detail.update(body)
        project_detail['updated_on'] = timestamp

        project_detail = json.loads(json.dumps(project_detail, cls=DecimalEncoder), parse_float=decimal.Decimal)
        logger.info(project_detail)
        dp_core_table.put_item(Item=project_detail)

        with dp_core_table.batch_writer() as batch:
            for team_id in add_teams:
                batch.put_item(Item={
                    "item_id": organization_id,
                    "object_type": 'relationship',
                    "sort_key": f'project:{project_id}:team:{team_id}',
                    "object_id": f'team:{team_id}:project:{project_id}',
                    "project_id": project_id,
                    "team_id": team_id,
                })

        with dp_core_table.batch_writer() as batch:
            for team_id in remove_teams:
                batch.delete_item(Key={'item_id': organization_id, 'sort_key': f'project:{project_id}:team:{team_id}'})

        event_item(
            body=project_detail, 
            event_id=event_id, 
            description=f'Updated project {project_detail["name"]}', 
            actor=user_id, 
            event_type='update', 
            ip_address=source_ip,
        )
        return response({"message": 'updated'}, None)
    elif http_method == 'GET':
        if path_parameters.get('projectId'):
            project_id = path_parameters['projectId']
            project_detail = dp_core_table.get_item(
                Key={"item_id": organization_id, "sort_key": f'project:{project_id}'},
                ProjectionExpression=','.join((*public_attributes, '#N', '#P')),
                ExpressionAttributeNames={"#N": "name", "#P": "public"}
            )['Item']

            if query_string_parameters.get('teams'):
                project_teams = dp_core_table.query(
                    KeyConditionExpression=Key('item_id').eq(organization_id) & Key('sort_key').begins_with(f'project:{project_id}:team:'),
                    ProjectionExpression='team_id'
                )['Items']
                project_team_ids = [team['team_id'] for team in project_teams]

                teams = []
                for team_id in project_team_ids:
                    team_detail = dp_core_table.get_item(
                        Key={"item_id": organization_id, "sort_key": f'team:{team_id}'},
                        ProjectionExpression=','.join(public_team_attributes),
                    )['Item']
                    teams.append(team_detail)
                project_detail['teams'] = teams

            if project_detail.get('photo', None):
                project_detail['photo_url'] = s3.generate_presigned_url('get_object', Params={'Bucket': 'developer-bucket', 'Key': f"private/{project_detail.get('photo')}"}, ExpiresIn=3600, HttpMethod='GET')
                
            logger.info(project_detail)

            return response(project_detail, None)
        else:
            limit = query_string_parameters.get('limit', 50)
            res = dp_core_table.query(
                IndexName='Object-Id-Index',
                KeyConditionExpression=Key('item_id').eq(organization_id) & Key('object_id').begins_with('project'),
                Limit=limit
            )

            projects = res['Items']
            for idx, item in enumerate(projects):
                if query_string_parameters.get('teams'):
                    project_teams = dp_core_table.query(
                        KeyConditionExpression=Key('item_id').eq(organization_id) & Key('sort_key').begins_with(f'project:{item["project_id"]}:team:'),
                        ProjectionExpression='team_id'
                    )['Items']
                    project_team_ids = [team['team_id'] for team in project_teams]
                    teams = []
                    for team_id in project_team_ids:
                        team_detail = dp_core_table.get_item(Key={"item_id": organization_id, "sort_key": f'team:{team_id}'})['Item']
                        teams.append(team_detail)
                    projects[idx]['teams'] = teams
                if item.get('photo', None):
                    projects[idx]['photo_url'] = s3.generate_presigned_url('get_object', Params={'Bucket': 'developer-bucket', 'Key': f"private/{item.get('photo')}"}, ExpiresIn=3600, HttpMethod='GET')    

            logger.info(projects)
            items = {'data': projects}
            if res.get('LastEvaluatedKey', None):
                items['pagination_key'] = res['LastEvaluatedKey']
                items['has_more'] = True
            else:
                items['has_more'] = False
            return response(items, None)
    elif http_method == 'DELETE':
        project_id = path_parameters['projectId']
        if organization_role == 'manager':
            project_teams = dp_core_table.query(
                KeyConditionExpression=Key('item_id').eq(organization_id) & Key('sort_key').begins_with(f'project:{project_id}:team:'),
                ProjectionExpression='team_id'
            )['Items']

            teams = []
            for team_id in project_teams:
                member_detail = dp_core_table.get_item(
                    Key={"item_id": organization_id, "sort_key": f'team:{team_id}:member:{user_id}'},
                ).get('Item')
                teams.append(member_detail)
            if len(teams) == 0:
                return response(None, {"message": "Not authorized"})

        project_items = dp_core_table.query(
            KeyConditionExpression=Key('item_id').eq(organization_id) & Key('sort_key').begins_with(f'project:{project_id}'),
        )

        with dp_core_table.batch_writer() as batch:
            for item in project_items['Items']:
                if item['sort_key'] == f'project:{project_id}': project_detail = item
                batch.delete_item(Key={'item_id': organization_id, 'sort_key': item['sort_key']})

        event_item(
            body=project_detail, 
            event_id=event_id, 
            description=f'Deleted project {project_detail["name"]}', 
            actor=user_id, 
            event_type='delete', 
            ip_address=source_ip,
        )
        return response({"message": 'deleted'}, None)
    else:
        return response(None, ValueError(f'Unsupported method :: {http_method}'))

    
